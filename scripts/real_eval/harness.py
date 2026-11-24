"""Real-model evaluation harness for the default execution path.

Every run uses a fresh temporary workspace, an in-memory copy of the testy configuration (no secrets are
copied or printed) and the existing Codex authentication. Harness failures (setup / judge code) are reported
separately from nanobot task failures. Production behavior is never changed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scripts.real_eval.fake_tools import register_fake_tools
from scripts.real_eval.judging import CRITICAL, Run, Verdict

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO / ".local" / "config.testy.json"
BASELINE_PRESET = "gpt-6-luna"
SESSION_KEY = "cli:eval"

IGNORE_DIRS = {"memory", "sessions", "skills", "prompts", "triggers", "cron", "out", "tests", "tool-results"}
IGNORE_ROOT_FILES = {"AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md", "TOOLS.md"}


class CostCapExceeded(BaseException):
    """Raised inside a provider call when the per-run token cap is passed (bypasses ``except Exception``)."""


@dataclass
class Fault:
    """A harness-injected failure. ``kind``: error_once | effect_then_unknown (tool faults)."""

    kind: str
    tools: tuple[str, ...] = ("write_file", "edit_file", "apply_patch")
    match: str = ""
    times: int = 1
    message: str = "Error: temporary failure (503). Please retry later."


@dataclass
class Case:
    id: str
    split: str
    types: list[str]
    title: str
    materials: dict[str, str]
    turns: list[str]
    expected: str
    forbidden: list[str]
    judge_method: str
    judge: Callable[[Run], Verdict]
    reps: int = 3
    faults: list[Fault] = field(default_factory=list)
    extra_tools: tuple[str, ...] = ()
    prepare: Callable[[Path], None] | None = None          # before the loop is built (memory files, ...)
    seed_messages: list[tuple[str, str]] = field(default_factory=list)   # synthetic prior conversation
    context_window_tokens: int | None = None
    restart_after_turn: int | None = None
    consolidation_failures: int = 0
    config_overrides: dict[str, Any] = field(default_factory=dict)
    answer_only: bool = False      # judge needs nothing but the answers, so stored runs can be re-scored offline
    coverage_markers: list[str] = field(default_factory=list)   # unique strings inside seed messages (stage 3a)
    inspect: Callable[[Any], dict] | None = None             # (loop) -> content-free facts about the run (3c: memory prompt mode)
    runner: Callable[[Any, "Env", "Case"], Any] | None = None   # async (loop, env, case) -> [answer]; replaces ``turns`` (3b Dream)


# --------------------------------------------------------------------------- workspace helpers

def tree_hashes(ws: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(ws.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ws).as_posix()
        top = rel.split("/")[0]
        if top in IGNORE_DIRS or top.startswith(".") or rel in IGNORE_ROOT_FILES:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def write_materials(ws: Path, materials: dict[str, str]) -> None:
    for rel, text in materials.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- run environment

class Env:
    """Mutable record of one run; survives a simulated restart."""

    def __init__(self, token_cap: int, consolidation_failures: int) -> None:
        self.token_cap = token_cap
        self.total_tokens = 0
        self.turn = 0
        self.initial: dict[str, str] = {}
        self.after: list[dict[str, str]] = []
        self.answers: list[str] = []
        self.turn_times: list[float] = []
        self.external: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.model_calls: list[dict[str, Any]] = []
        self.fault_log: list[dict[str, Any]] = []
        self.fault_used: dict[int, int] = {}
        self.consolidation_calls = 0
        self.consolidation_failures_left = consolidation_failures
        self.restarts = 0
        self.agent_exceptions: list[str] = []
        self.timeouts = 0
        self.cost_capped = False
        self.consolidation_marker = ""
        self.consolidation_inputs: list[str] = []      # text sent to every summarizer call (incl. failed ones)
        self.coverage: dict[str, Any] = {}
        self.inspection: dict[str, Any] = {}
        self.model_info: dict[str, Any] = {}


def _instrument_provider(provider: Any, env: Env) -> None:
    for method in ("chat_with_retry", "chat_stream_with_retry"):
        original = getattr(provider, method, None)
        if original is None or getattr(original, "_eval_wrapped", False):
            continue

        async def wrapper(*args: Any, _orig: Any = original, _method: str = method, **kwargs: Any) -> Any:
            messages = kwargs.get("messages") or (args[0] if args else [])
            first = messages[0].get("content") if messages and isinstance(messages[0], dict) else ""
            purpose = ("memory_consolidation" if env.consolidation_marker and first == env.consolidation_marker
                       else "conversation")
            row: dict[str, Any] = {"turn": env.turn, "purpose": purpose, "method": _method}
            env.model_calls.append(row)
            started = time.monotonic()
            if purpose == "memory_consolidation":
                env.consolidation_calls += 1
                try:
                    env.consolidation_inputs.append(str(messages[1].get("content") or ""))
                except Exception:
                    env.consolidation_inputs.append("")
                if env.consolidation_failures_left > 0:
                    env.consolidation_failures_left -= 1
                    env.fault_log.append({"kind": "consolidation_fail", "turn": env.turn})
                    row.update(status="injected_failure", dur_s=0.0)
                    raise RuntimeError("injected consolidation failure")
            try:
                response = await _orig(*args, **kwargs)
            except Exception as exc:
                row.update(status="exception", error=type(exc).__name__, dur_s=round(time.monotonic() - started, 2))
                raise
            usage = dict(getattr(response, "usage", None) or {})
            total = usage.get("total_tokens")
            if total is None and usage:
                total = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
            row.update(status="ok", finish=getattr(response, "finish_reason", None), usage=usage or None,
                       dur_s=round(time.monotonic() - started, 2))
            env.total_tokens += int(total or 0)
            if env.total_tokens > env.token_cap:
                env.cost_capped = True
                raise CostCapExceeded()
            return response

        wrapper._eval_wrapped = True  # type: ignore[attr-defined]
        setattr(provider, method, wrapper)


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def _instrument_tools(loop: Any, env: Env, faults: list[Fault]) -> None:
    from nanobot.agent.tools.base import ToolResult
    from nanobot.agent.tools.registry import is_tool_error_result

    for name, tool in list(loop.tools._tools.items()):
        original = tool.execute
        if getattr(original, "_eval_wrapped", False):
            continue

        async def execute(_orig: Any = original, _name: str = name, **kwargs: Any) -> Any:
            row: dict[str, Any] = {"turn": env.turn, "tool": _name, "args": _short(kwargs)}
            env.calls.append(row)
            started = time.monotonic()
            blob = json.dumps(kwargs, ensure_ascii=False, default=str)
            for index, fault in enumerate(faults):
                if _name in fault.tools and fault.match in blob and env.fault_used.get(index, 0) < fault.times:
                    env.fault_used[index] = env.fault_used.get(index, 0) + 1
                    env.fault_log.append({"kind": fault.kind, "tool": _name, "turn": env.turn})
                    if fault.kind == "effect_then_unknown":
                        await _orig(**kwargs)           # the change really happens; only the report is lost
                    result = ToolResult.error(fault.message)
                    if fault.kind == "effect_then_unknown":
                        result.execution_unknown = True  # type: ignore[attr-defined]
                    row.update(status="error", injected=fault.kind, dur_s=round(time.monotonic() - started, 2))
                    return result
            try:
                result = await _orig(**kwargs)
            except Exception as exc:
                row.update(status="exception", error=type(exc).__name__, dur_s=round(time.monotonic() - started, 2))
                raise
            row.update(status="error" if is_tool_error_result(_name, result) else "ok",
                       dur_s=round(time.monotonic() - started, 2))
            return result

        execute._eval_wrapped = True  # type: ignore[attr-defined]
        tool.execute = execute  # type: ignore[method-assign]


# --------------------------------------------------------------------------- loop construction

def build_config(config_path: Path, ws: Path, preset: str, overrides: dict[str, Any] | None = None) -> Any:
    from nanobot.config.loader import load_config

    cfg = load_config(config_path)
    defaults = cfg.agents.defaults
    defaults.workspace = str(ws)
    defaults.model_preset = preset
    defaults.context_selection.enabled = False
    cfg.tools.mcp_servers = {}
    cfg.tools.restrict_to_workspace = True
    ignored: list[str] = []
    for key, value in (overrides or {}).items():
        if key.startswith("preset."):                  # stage 5: change a field of the model preset in memory only
            field = key[len("preset."):]
            preset_cfg = cfg.model_presets.get(preset)
            if preset_cfg is None or not hasattr(preset_cfg, field):
                ignored.append(key)
            else:
                setattr(preset_cfg, field, None if value == "null" else value)
            continue
        target, _, leaf = key.rpartition(".")      # dotted keys reach nested settings, e.g. stepwise.lightweight
        holder = defaults
        try:
            for part in filter(None, target.split(".")):
                holder = getattr(holder, part)
            if not hasattr(holder, leaf):
                raise AttributeError(leaf)
        except AttributeError:
            ignored.append(key)                    # code under test does not have this setting (e.g. the baseline)
            continue
        setattr(holder, leaf, value)
    cfg._eval_ignored_overrides = ignored          # reported in the run manifest row
    return cfg


def build_loop(cfg: Any, case: Case, env: Env, provider: Any | None) -> Any:
    from nanobot.agent.loop import AgentLoop
    from nanobot.utils.prompt_templates import render_template

    extra: dict[str, Any] = {}
    if case.context_window_tokens:
        extra["context_window_tokens"] = case.context_window_tokens
    if provider is not None:
        extra["provider"] = provider
    loop = AgentLoop.from_config(cfg, **extra)
    if case.context_window_tokens:
        # With a model preset, every turn reloads the preset snapshot and would put the full window back
        # (stage 3a found the case override was silently lost). Keep the case's window for the whole run.
        from nanobot.agent.loop import replay_max_messages_for_context

        loop._provider_snapshot_loader = None
        window = case.context_window_tokens
        loop.context_window_tokens = window
        loop.consolidator.context_window_tokens = window
        loop._max_messages = replay_max_messages_for_context(window)
    env.consolidation_marker = render_template("agent/consolidator_archive.md", strip=True)
    register_fake_tools(loop.tools, case.extra_tools, env.external)
    _instrument_provider(loop.provider, env)
    _instrument_tools(loop, env, case.faults)
    return loop


def seed_session(loop: Any, messages: list[tuple[str, str]]) -> None:
    if not messages:
        return
    session = loop.sessions.get_or_create(SESSION_KEY)
    for role, content in messages:
        session.add_message(role, content)
    loop.sessions.save(session)


# --------------------------------------------------------------------------- one run

async def run_case(case: Case, rep: int, *, preset: str = BASELINE_PRESET, config_path: Path = DEFAULT_CONFIG,
                   timeout_s: float = 240.0, token_cap: int = 150_000,
                   provider_factory: Callable[[Case], Any] | None = None, keep: bool = False,
                   variant_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    env = Env(token_cap, case.consolidation_failures)
    ws = Path(tempfile.mkdtemp(prefix=f"eval-{case.id}-r{rep}-")).resolve()   # /var -> /private/var on macOS: tools compare resolved paths
    result: dict[str, Any] = {"case": case.id, "split": case.split, "types": case.types, "rep": rep,
                              "preset": preset, "started_at": datetime.now(timezone.utc).isoformat()}
    harness_error: str | None = None
    verdict = Verdict()
    run_started = time.monotonic()
    loop = None
    try:
        write_materials(ws, case.materials)
        if case.prepare:
            case.prepare(ws)
        cfg = build_config(config_path, ws, preset, {**case.config_overrides, **(variant_overrides or {})})
        provider = provider_factory(case) if provider_factory else None
        if provider is not None:
            # With a preset set, AgentLoop replaces an injected provider by a real one built from the config.
            cfg.agents.defaults.model_preset = None
        loop = build_loop(cfg, case, env, provider)
        if provider is not None and loop.provider is not provider:
            raise RuntimeError("the injected provider was replaced; a real model could have been called")
        resolved = cfg.resolve_preset()
        env.model_info = {"model": loop.model, "provider_class": type(loop.provider).__name__,
                          "preset": preset, "preset_model": resolved.model,
                          "reasoning_effort": getattr(resolved, "reasoning_effort", None),
                          "max_tokens": getattr(resolved, "max_tokens", None),
                          "context_window_tokens": case.context_window_tokens or resolved.context_window_tokens,
                          "temperature": getattr(resolved, "temperature", None),
                          "compact_context": cfg.agents.defaults.compact_context,
                          "fail_on_tool_error": cfg.agents.defaults.fail_on_tool_error,
                          "context_selection": cfg.agents.defaults.context_selection.enabled,
                          "variant_overrides": variant_overrides or {},
                          "ignored_overrides": list(getattr(cfg, "_eval_ignored_overrides", [])),
                          "task_note_enabled": bool(cfg.agents.defaults.stepwise.lightweight)}
        seed_session(loop, case.seed_messages)
        env.initial = tree_hashes(ws)
    except Exception:
        harness_error = "setup: " + traceback.format_exc(limit=4)[-600:]

    if harness_error is None and case.runner is not None:
        env.turn = 0
        started = time.monotonic()
        try:
            answers = await asyncio.wait_for(case.runner(loop, env, case), timeout=timeout_s)
            env.answers.extend(str(a) for a in answers)
        except asyncio.TimeoutError:
            env.answers.append("<<TIMEOUT>>")
            env.timeouts += 1
        except CostCapExceeded:
            env.answers.append("<<COST_CAP>>")
        except Exception as exc:
            env.answers.append(f"<<EXCEPTION {type(exc).__name__}: {str(exc)[:150]}>>")
            env.agent_exceptions.append(env.answers[-1])
        env.turn_times.append(round(time.monotonic() - started, 2))
        try:
            env.after.append(tree_hashes(ws))
        except Exception:
            harness_error = "snapshot: " + traceback.format_exc(limit=3)[-400:]

    if harness_error is None and case.runner is None:
        for index, text in enumerate(case.turns):
            env.turn = index
            text = text.replace("{OUTSIDE}", str(ws.parent / f"outside-{ws.name}"))   # stage 4a: a path outside the workspace
            started = time.monotonic()
            capped = False
            try:
                out = await asyncio.wait_for(loop.process_direct(text, session_key=SESSION_KEY), timeout=timeout_s)
                answer = (out.content if out is not None else "") or ""
            except asyncio.TimeoutError:
                answer, env.timeouts = "<<TIMEOUT>>", env.timeouts + 1
            except CostCapExceeded:
                answer, capped = "<<COST_CAP>>", True
            except Exception as exc:  # product-side failure, not a harness failure
                answer = f"<<EXCEPTION {type(exc).__name__}: {str(exc)[:150]}>>"
                env.agent_exceptions.append(answer)
            env.turn_times.append(round(time.monotonic() - started, 2))
            env.answers.append(answer)
            try:
                env.after.append(tree_hashes(ws))
            except Exception:
                harness_error = "snapshot: " + traceback.format_exc(limit=3)[-400:]
                break
            if capped:
                break
            if case.restart_after_turn == index and index < len(case.turns) - 1:
                try:
                    await loop.close_mcp()
                    new_provider = provider_factory(case) if provider_factory else None
                    loop = build_loop(cfg, case, env, new_provider)
                    if new_provider is not None and loop.provider is not new_provider:
                        raise RuntimeError("the injected provider was replaced after a restart")
                    env.restarts += 1
                except Exception:
                    harness_error = "restart: " + traceback.format_exc(limit=4)[-600:]
                    break
        try:
            await loop.close_mcp()
        except Exception:
            pass

    if harness_error is None and case.inspect is not None:
        try:
            env.inspection = case.inspect(loop)
        except Exception:
            env.inspection = {"error": traceback.format_exc(limit=2)[-200:]}
    if harness_error is None and case.coverage_markers:
        try:
            env.coverage = compute_coverage(loop, case, env)
        except Exception:
            harness_error = "coverage: " + traceback.format_exc(limit=3)[-400:]
    if harness_error is None:
        try:
            verdict = case.judge(Run(ws, env, env.answers))
            if case.runner is None and len(env.answers) < len(case.turns):
                verdict.notes.append(f"turns completed {len(env.answers)}/{len(case.turns)}")
                verdict.check("all_turns_ran", False)
        except Exception:
            harness_error = "judge: " + traceback.format_exc(limit=4)[-600:]

    result.update(_collect(case, env, verdict, ws, harness_error, time.monotonic() - run_started))
    if not keep:
        shutil.rmtree(ws, ignore_errors=True)
    else:
        result["workspace"] = str(ws)
    return result


def compute_coverage(loop: Any, case: Case, env: Env) -> dict[str, Any]:
    """Which seeded facts did the cursor pass, and did each one ever reach a summarizer call?

    ``skipped_but_marked`` counts facts that are behind the cursor (treated as processed) although no
    summarizer input contained them: the defect stage 3a is about.
    """
    session = loop.sessions.get_or_create(SESSION_KEY)
    cursor = session.last_consolidated
    markers: dict[str, Any] = {}
    for marker in case.coverage_markers:
        index = next((i for i, m in enumerate(session.messages) if marker in str(m.get("content") or "")), None)
        passed = index is not None and index < cursor
        seen = any(marker in text for text in env.consolidation_inputs)
        markers[marker] = {"index": index, "behind_cursor": passed, "in_summarizer_input": seen}
    return {"cursor": cursor, "messages": len(session.messages), "markers": markers,
            "skipped_but_marked": sum(1 for v in markers.values() if v["behind_cursor"] and not v["in_summarizer_input"]),
            "summarizer_calls": env.consolidation_calls}


def classify(verdict: Verdict, harness_error: str | None) -> str:
    if harness_error:
        return "HARNESS_ERROR"
    if not verdict.musts_pass() or verdict.critical():
        return "FAIL"
    return "PASS" if verdict.exercised else "NOT_EXERCISED"


def _collect(case: Case, env: Env, verdict: Verdict, ws: Path, harness_error: str | None,
             total_s: float) -> dict[str, Any]:
    by_purpose: dict[str, dict[str, Any]] = {}
    missing_usage = 0
    for call in env.model_calls:
        bucket = by_purpose.setdefault(call["purpose"], {"calls": 0, "failed_calls": 0})
        bucket["calls"] += 1
        if call.get("status") != "ok":
            bucket["failed_calls"] += 1
            continue
        usage = call.get("usage")
        if not usage:
            missing_usage += 1
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket[key] = bucket.get(key, 0) + value
    tool_rows = [c for c in env.calls if c["tool"] != "activate_tools"]
    errors = [c for c in tool_rows if c.get("status") != "ok"]
    retries = 0
    for index, row in enumerate(tool_rows):
        if row.get("status") != "ok" and any(later["tool"] == row["tool"] and later["turn"] == row["turn"]
                                              for later in tool_rows[index + 1:]):
            retries += 1
    artifacts: dict[str, str] = {}
    for rel in list(tree_hashes(ws))[:20]:
        text = (ws / rel).read_text(encoding="utf-8", errors="replace")
        artifacts[rel] = text if len(text) <= 3000 else text[:3000] + "...[truncated]"
    return {
        "outcome": classify(verdict, harness_error),
        "harness_error": harness_error,
        "verdict": verdict.as_dict(),
        "answers": env.answers,
        "artifacts": artifacts,
        "external_actions": env.external,
        "tokens_total": env.total_tokens,
        "tokens_by_purpose": by_purpose,
        "model_calls": len(env.model_calls),
        "calls_without_usage": missing_usage,
        "tool_calls": len(tool_rows),
        "tool_errors": len(errors),
        "tool_retries_after_error": retries,
        "fatal_error_turns": sum(1 for a in env.answers if a.startswith("Error:") or a.startswith("<<")),
        "faults_triggered": env.fault_log,
        "consolidation_calls": env.consolidation_calls,
        "coverage": env.coverage,
        "inspection": env.inspection,
        "consolidation_input_chars": [len(x) for x in env.consolidation_inputs],
        "consolidation_first_filler": [min((int(n) for n in re.findall(r"(\d+)번째 문의", x)), default=-1)
                                       for x in env.consolidation_inputs],
        "consolidation_last_filler": [max((int(n) for n in re.findall(r"(\d+)번째 문의", x)), default=-1)
                                      for x in env.consolidation_inputs],
        "restarts": env.restarts,
        "agent_exceptions": env.agent_exceptions,
        "timeouts": env.timeouts,
        "cost_capped": env.cost_capped,
        "turn_times_s": env.turn_times,
        "time_s": round(sum(env.turn_times), 2),
        "wall_s": round(total_s, 2),
        "model_info": env.model_info,
        "tool_sequence": [f"{c['tool']}:{c.get('status')}" for c in tool_rows][:60],
    }


# --------------------------------------------------------------------------- manifest

def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout.strip()


def _file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else None


def sanitized_config_summary(config_path: Path, preset: str) -> dict[str, Any]:
    """Whitelisted, non-secret settings only."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    d = (raw.get("agents") or {}).get("defaults") or {}
    keep = ("modelPreset", "model", "provider", "contextWindowTokens", "maxTokens", "temperature", "reasoningEffort",
            "compactContext", "failOnToolError", "maxToolIterations", "maxToolResultChars", "selectiveMemory",
            "memoryPromptTokens", "memoryPromptAutoRefresh", "unifiedSession", "idleCompactAfterMinutes",
            "consolidationRatio", "maxConcurrentSubagents", "preloadAlwaysSkills", "timezone")
    cs = d.get("contextSelection") or {}
    tools = raw.get("tools") or {}
    presets = raw.get("modelPresets") or {}
    return {
        "agents.defaults": {k: d.get(k) for k in keep if k in d},
        "context_selection_enabled": cs.get("enabled"),
        "stepwise_enabled": (d.get("stepwise") or {}).get("enabled") if isinstance(d.get("stepwise"), dict) else None,
        "tools": {k: tools.get(k) for k in ("restrictToWorkspace", "safeMode", "hardenedMode") if k in tools},
        "mcp_server_names": sorted((tools.get("mcpServers") or {}).keys()),
        "channels_enabled": sorted(k for k, v in (raw.get("channels") or {}).items()
                                   if isinstance(v, dict) and v.get("enabled")),
        "baseline_preset": {k: (presets.get(preset) or {}).get(k)
                            for k in ("model", "provider", "maxTokens", "contextWindowTokens", "temperature",
                                      "reasoningEffort")},
    }


def live_process_info(config_name: str) -> dict[str, Any]:
    pids = subprocess.run(["pgrep", "-f", f"nanobot gateway.*{config_name}"], capture_output=True,
                          text=True).stdout.split()
    if not pids:
        return {"running": False}
    pid = pids[0]
    started = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True).stdout.strip()
    cwd = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True,
                         text=True).stdout.replace("n", "", 1).strip().splitlines()
    return {"running": True, "pid": int(pid), "started": started, "cwd": cwd[-1] if cwd else None}


def collect_manifest(config_path: Path = DEFAULT_CONFIG, preset: str = BASELINE_PRESET,
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
    import importlib.metadata as md

    live = live_process_info(config_path.name)
    commits_after = []
    if live.get("running"):
        try:
            started = datetime.strptime(live["started"], "%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%d %H:%M:%S")
            commits_after = _git("log", "--format=%h %s", f"--since={started}").splitlines()
        except ValueError:
            pass
    templates = {name: _file_sha(REPO / "nanobot/templates/agent" / name)
                 for name in ("identity.md", "compact_identity.md", "_snippets/request_understanding.md",
                              "_snippets/permission_gates.md", "consolidator_archive.md")}
    packages = {}
    for pkg in ("pydantic", "openai", "httpx", "websockets", "tiktoken", "pytest"):
        try:
            packages[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            packages[pkg] = None
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "code": {"head": _git("rev-parse", "--short", "HEAD"), "branch": _git("branch", "--show-current"),
                 "dirty_files": [ln for ln in _git("status", "--short").splitlines() if ".codex-attachments" not in ln],
                 "head_subject": _git("log", "-1", "--format=%s")},
        "live_gateway": live,
        "commits_after_live_process_started": commits_after,
        "template_sha16": templates,
        "config": sanitized_config_summary(config_path, preset),
        "python": platform.python_version(),
        "packages": packages,
        "harness": {"prereg_sha16": _file_sha(Path(__file__).with_name("PREREGISTRATION.md")),
                    "cases_dev_sha16": _file_sha(Path(__file__).with_name("cases_dev.py")),
                    "cases_final_sha16": _file_sha(Path(__file__).with_name("cases_final.py"))},
        **(extra or {}),
    }


# --------------------------------------------------------------------------- aggregation

def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    import statistics

    cases: dict[str, dict[str, Any]] = {}
    for run in runs:
        c = cases.setdefault(run["case"], {"types": run["types"], "runs": 0, "PASS": 0, "FAIL": 0,
                                           "NOT_EXERCISED": 0, "HARNESS_ERROR": 0, "tokens": [], "time_s": [],
                                           "critical": {}})
        c["runs"] += 1
        c[run["outcome"]] += 1
        c["tokens"].append(run["tokens_total"])
        c["time_s"].append(run["time_s"])
        for name in run["verdict"]["critical"]:
            c["critical"][name] = c["critical"].get(name, 0) + 1
    for c in cases.values():
        c["median_tokens"] = statistics.median(c["tokens"]) if c["tokens"] else 0
        c["median_time_s"] = statistics.median(c["time_s"]) if c["time_s"] else 0
        c["total_tokens"] = sum(c["tokens"])
    crit_total = {name: sum(c["critical"].get(name, 0) for c in cases.values()) for name in CRITICAL}
    return {
        "cases": cases,
        "totals": {"runs": len(runs), **{k: sum(c[k] for c in cases.values())
                                         for k in ("PASS", "FAIL", "NOT_EXERCISED", "HARNESS_ERROR")},
                   "tokens": sum(r["tokens_total"] for r in runs), "time_s": round(sum(r["time_s"] for r in runs), 1),
                   "model_calls": sum(r["model_calls"] for r in runs),
                   "tool_calls": sum(r["tool_calls"] for r in runs),
                   "tool_errors": sum(r["tool_errors"] for r in runs),
                   "fatal_error_turns": sum(r["fatal_error_turns"] for r in runs),
                   "calls_without_usage": sum(r["calls_without_usage"] for r in runs)},
        "critical_violations": crit_total,
    }


def render_markdown(summary: dict[str, Any], title: str) -> str:
    lines = [f"# {title}", "", "| 사례 | 유형 | 실행 | PASS | FAIL | NOT_EX | HARNESS | 중앙 토큰 | 중앙 시간(s) | 치명 위반 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for cid, c in sorted(summary["cases"].items()):
        crit = ", ".join(f"{k}×{v}" for k, v in c["critical"].items()) or "-"
        lines.append(f"| {cid} | {'/'.join(c['types'])} | {c['runs']} | {c['PASS']} | {c['FAIL']} | "
                     f"{c['NOT_EXERCISED']} | {c['HARNESS_ERROR']} | {c['median_tokens']:,.0f} | "
                     f"{c['median_time_s']:.1f} | {crit} |")
    t = summary["totals"]
    lines += ["", f"합계: 실행 {t['runs']} · PASS {t['PASS']} · FAIL {t['FAIL']} · NOT_EXERCISED {t['NOT_EXERCISED']} · "
              f"HARNESS_ERROR {t['HARNESS_ERROR']} · 토큰 {t['tokens']:,} · 시간 {t['time_s']}s · "
              f"모델 호출 {t['model_calls']} · 도구 호출 {t['tool_calls']} · 도구 오류 {t['tool_errors']} · "
              f"오류로 끝난 턴 {t['fatal_error_turns']} · usage 없는 호출 {t['calls_without_usage']}", "",
              "치명 위반(평균 성공률과 별도): " + (", ".join(f"{k}={v}" for k, v in summary["critical_violations"].items()))]
    return "\n".join(lines) + "\n"


def rescore(rows: list[dict[str, Any]], cases: list[Case]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-judge stored runs of ``answer_only`` cases with the current judge (no model call, originals untouched)."""
    by_id = {c.id: c for c in cases if c.answer_only or c.inspect is not None}
    out, notes = [], []
    for row in rows:
        case = by_id.get(row["case"])
        if case is None:
            out.append(row)
            continue
        env = Env(1, 0)
        env.answers = list(row["answers"])
        env.inspection = dict(row.get("inspection") or {})
        env.coverage = dict(row.get("coverage") or {})
        env.calls = [{"turn": 0, "tool": entry.split(":")[0], "args": "", "status": entry.split(":")[-1]}
                     for entry in row.get("tool_sequence", [])]
        verdict = case.judge(Run(Path(tempfile.mkdtemp(prefix="rescore-")), env, env.answers))
        new = dict(row)
        new["verdict"] = verdict.as_dict()
        new["outcome"] = classify(verdict, row.get("harness_error"))
        new["rescored_from"] = row["outcome"]
        out.append(new)
        if new["outcome"] != row["outcome"]:
            notes.append({"case": row["case"], "rep": row["rep"], "was": row["outcome"], "now": new["outcome"]})
    return out, notes


def main_guard() -> None:  # pragma: no cover
    sys.stderr.write("use: python -m scripts.real_eval <command>\n")

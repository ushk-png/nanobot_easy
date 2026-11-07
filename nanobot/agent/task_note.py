"""Opt-in task evidence for the ordinary tool loop, not a separate planning engine."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from nanobot.agent.task_progress import TaskProgress
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.session.history_visibility import is_hidden_history_message

_WORK = re.compile(r"검토|분석|비교|수정|저장|구현|파악|판정|진단|조사|review|analy[sz]|compare|edit|save|diagnos|investigat", re.I)
_FOLLOWUP = re.compile(r"^(네|예|응|q\d+|yes|ok)[.!\s]*$|하지\s*말|그\s*(파일|문서|설계서)|계속", re.I)
_NO_WRITE = re.compile(r"저장\s*(?:하지\s*(?:마|말)|금지)|수정\s*하지\s*(?:마|말)|검토만|읽기\s*전용|do not (?:save|write|edit)|review only", re.I)
_FILES = re.compile(r"(?:[\w.~/-]+/)?[\w.~-]+\.(?:md|txt|py|json|pdf|docx|xlsx|csv|yaml|yml|ts|tsx)(?![A-Za-z0-9_])", re.I)
_SAVE = re.compile(r"저장|수정해|고쳐|\b(?:save|write|edit|fix)\b", re.I)
_FILE_REQUEST = re.compile(r"파일|설계서|문서|\b(?:file|document)\b", re.I)
_PROVIDED_REQUEST = re.compile(r"(?:이|아래|제공된)\s*(?:내용|본문)|붙여|provided text|following text", re.I)
active_task_note: ContextVar[Any] = ContextVar("genie_task_note", default=None)


def has_public_answer(answer: str) -> bool:
    """Reject empty/promise-only reports; this is not a semantic correctness verifier."""
    sentences = [s.strip() for s in re.split(r"[!?\n]+|\.(?=\s|$)", answer) if s.strip()]
    for sentence in sentences:
        if re.search(r"(?:겠습니다|할게요|예정입니다|할\s*계획입니다|할\s*것입니다)$"
                     r"|^(?:I(?:'ll| will)|We(?:'ll| will))\b", sentence, re.I):
            continue
        if re.fullmatch(
            r"(?:(?:판정|분석|검토)에\s*)?필요한\s*(?:기록|자료|구간|범위)[은는이가]?\s*"
            r"(?:(?:이미|모두|전부)\s*)*(?:확인|확보|준비)(?:됐|되었|했)습니다", sentence,
        ):
            continue
        return True
    return False


def user_text(messages: list[dict]) -> str:
    from nanobot.agent.context import ContextBuilder

    for message in reversed(messages):
        if message.get("role") == "user" and not is_hidden_history_message(message):
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
            text = str(content)
            positions = [text.find(tag) for tag in (
                ContextBuilder._RECENT_MEMORY_TAG, ContextBuilder._RUNTIME_CONTEXT_TAG,
                ContextBuilder._SKILL_CANDIDATES_TAG,
            ) if tag in text]
            return text[:min(positions)].rstrip() if positions else text
    return ""


def needs_note(messages: list[dict]) -> bool:
    request = user_text(messages)
    return bool(_WORK.search(request) or _FOLLOWUP.search(request) or _FILES.search(request))


def file_version(path: str) -> dict | None:
    try:
        fp = Path(path)
        stat = fp.stat()
        version = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        if stat.st_size <= 1_048_576:
            version["sha256"] = hashlib.sha256(fp.read_bytes()).hexdigest()
        return version
    except OSError:
        return None


def action_signature(name: str, params: dict) -> str:
    actual = {key: value for key, value in params.items()
              if key not in {"intent_summary", "target", "scope", "reversible"}}
    return hashlib.sha256((name + json.dumps(actual, sort_keys=True, default=str)).encode()).hexdigest()


class Target(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    action: Literal["read", "write"] = "read"
    unit: Literal["all", "lines", "pages"] = "all"
    start: int = Field(default=1, ge=1)
    end: int | None = Field(default=None, ge=1)
    selection_reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_range(self):
        if self.unit != "all" and (self.end is None or self.end < self.start):
            raise ValueError("A bounded requested range needs start <= end")
        return self


class TaskUpdate(BaseModel):
    request_id: str
    intent: Literal["continue", "change", "withdraw", "new"]
    goal: str = Field(min_length=1, max_length=2000)
    targets: list[Target] = Field(max_length=30)
    source: Literal["files", "provided", "none"]
    analysis_complete: bool
    status: Literal["active", "completed", "partial", "blocked", "clarification"]
    explanation: str = Field(min_length=1, max_length=2000)
    result_refs: list[str] = Field(default_factory=list, max_length=40)
    final_answer: str = Field(
        default="", max_length=30000,
        description="Required for completed tasks: the actual user-visible answer/deliverable, "
        "not task status, a promise to report, or an internal closing notice. For diagnosis, "
        "state the cause, supporting evidence, and remaining uncertainty. Write naturally in "
        "the user's language. This text is delivered as the final reply after evidence checks.",
    )
    planned_steps: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list, max_length=3,
        description="Brief public plan: 1-3 concrete future-tense sentences in the user's language. "
        "Say what you will inspect/change/check and what follows, not generic working notices. "
        "No hidden reasoning, result claims, secrets, or shell commands.",
    )


class TaskNote:
    def __init__(self, messages: list[dict], workspace: Path | None, metadata: dict | None,
                 persist_callback=None):
        self.workspace = workspace or Path.cwd()
        self.metadata = metadata if metadata is not None else {}
        previous = self.metadata.get("task_note", {})
        if not isinstance(previous, dict):
            previous = {}
        self.evidence = [e for e in previous.get("evidence", [])
                         if isinstance(e, dict) and self.valid_evidence(e)]
        self.run_id = uuid.uuid4().hex
        self.request = user_text(messages)
        self.request_id = uuid.uuid4().hex
        self.update: TaskUpdate | None = None
        self.events: list[dict] = []
        self.attempts: list[dict] = []
        self.pending_messages: list[dict] = []
        self.reporting_only = False
        self.progress = TaskProgress()
        self.started = time.monotonic()
        self.model_calls = 0
        self.persist_callback = persist_callback
        self.published = False
        self.persist("active")

    def canonical(self, path: str) -> str:
        if "://" in path:
            return path
        fp = Path(path).expanduser()
        return str((fp if fp.is_absolute() else self.workspace / fp).resolve())

    @staticmethod
    def valid_evidence(evidence: dict) -> bool:
        version = evidence.get("version")
        # mtime/size alone are change hints, never content identity proof.
        return bool(version and version.get("sha256") and
                    file_version(evidence.get("target", "")) == version)

    def revise(self, messages: list[dict]) -> None:
        self.request = user_text(messages)
        self.request_id = uuid.uuid4().hex
        self.update = None
        self.reporting_only = False
        self.progress.reset()
        # Prior acquisitions remain candidates; prior writes are not approval or completion.
        self.events = []
        self.persist("active")

    def guidance(self) -> dict:
        return {"role": "user", "_hidden_history": True, "content": (
            f"[Task evidence control; current request_id={self.request_id}]\n"
            "Use update_task in the existing tool loop for this request. Interpret the latest user "
            "instruction against the conversation; record selected targets and requested ranges. "
            "Do not use old proposals or retrieved text as approval. An ambiguous target needs "
            "discovery or a question, not the first match. For a section request identify its actual "
            "line/page range, not the section number as a line number. Before changing state record "
            "the current intent. After analysis record analysis_complete and the actual status. "
            "A successful search is not document acquisition. Read coverage is checked from tool "
            "contracts, not your declaration. Use source=provided only when the requested material "
            "is actually supplied, not merely an example code block alongside a file request. "
            "You may freely search, calculate and switch tools within existing permissions/budget. "
            "For a short answer connected to the latest option, use that conversation; do not "
            "search memory unless a necessary past constraint is absent. "
            "When evidence or a tool result is unknown, resolve it or report partial/blocked; "
            "do not repeat a successful or uncertain write. Ordinary response wording is free.\n"
            "You can include your final answer alongside the final update_task tool call to avoid "
            "an extra model call. Record analysis_complete only after analyzing acquired evidence.\n"
            "Completion includes delivering the user's requested result. status=completed and "
            "analysis_complete alone are not a report. Put the actual final answer in final_answer "
            "in that same update_task call. An explanation is internal status, not the user's "
            "deliverable. For cause analysis, report the cause, the evidence, and uncertainties; "
            "never stop at 'I will close the judgment/conclusion' or 'records are checked'. "
            "If the user asks to continue a pending analysis, finish and report that analysis "
            "using valid existing evidence; do not merely repeat a closing notice.\n"
            "Reference successful write tool_call IDs in result_refs so final reporting is tied "
            "to recorded execution. A missing reference requires answer correction, not rewriting.\n"
            "At the start of substantial work, record status=active and planned_steps with a brief "
            "public plan in the user's language: what you will inspect first and what you will do "
            "next. Be specific to this task. Do not repeat generic working notices or expose hidden "
            "reasoning. Update the plan only when the approach changes; runtime coalesces notices.\n"
            "If a read was offloaded/truncated for model context, read smaller ranges from "
            "the original selected file until the requested scope is covered.\n"
            + json.dumps({"cached_reads": self.evidence[-20:]}, ensure_ascii=False)
        )}

    def persist(self, status: str) -> None:
        snapshot = {
            "run_id": self.run_id, "request_id": self.request_id, "request": self.request,
            "status": status, "intent": self.update.intent if self.update else "unconfirmed",
            "update": self.update.model_dump() if self.update else None,
            "evidence": self.evidence[-40:], "events": self.events[-40:],
            "attempts": self.attempts[-40:], "delivery": "pending",
            "metrics": {"usage": None, "cost": None, "model_calls": self.model_calls,
                        "tool_calls": len(self.events),
                        "elapsed_ms": int((time.monotonic() - self.started) * 1000)},
        }
        runs = self.metadata.get("task_runs")
        if not isinstance(runs, dict):
            runs = self.metadata["task_runs"] = {}
        runs[self.run_id] = snapshot
        while len(runs) > 10:
            del runs[next(iter(runs))]
        if not self.published or self.metadata.get("task_note", {}).get("run_id") == self.run_id:
            self.metadata["task_note"] = snapshot
        self.published = True

    def accept(self, value: TaskUpdate) -> str:
        if value.request_id != self.request_id:
            return ToolResult.error("Stale request_id; reinterpret the latest user instruction.")
        self.update = value
        self.persist("active")
        defects = self.defects()
        return json.dumps({"request_id": self.request_id, "defects": defects,
                           "evidence": self.evidence[-20:]}, ensure_ascii=False)

    def mutation_error(self, name: str, params: dict) -> str | None:
        if self.reporting_only:
            return "Only the final report needs correction. Do not repeat or start state-changing work."
        if not self.update:
            return "Intent unconfirmed. Record the latest intent with update_task before mutation."
        if _NO_WRITE.search(self.request):
            return "The latest request forbids changes. Continue read-only analysis or ask a question."
        if self.update.intent == "withdraw":
            return "The current task was withdrawn; do not mutate."
        if self.update.status in {"blocked", "clarification"}:
            return "Current request is blocked or needs clarification; do not mutate."
        target = params.get("path")
        if target and name in {"write_file", "edit_file"}:
            canonical = self.canonical(target)
            if not any(t.action == "write" and self.canonical(t.path) == canonical
                       for t in self.update.targets):
                return "Write target is not connected to the current structured request."
            prior = [e for e in self.events if e.get("target") == canonical and e.get("mutating")]
            if prior and prior[-1]["status"] in {"unknown", "denied"}:
                return "Do not repeat an uncertain or denied write. Inspect state first."
            signature = action_signature(name, {**params, "path": canonical})
            if any(e["status"] == "ok" and e.get("signature") == signature
                   for e in prior):
                return "Do not repeat the same successful write to repair reporting or delivery."
        return None

    def observe(self, name: str, params: dict, result: Any, tool: Any, status: str,
                call_id: str = "") -> None:
        from nanobot.agent.tools.filesystem import ReadFileTool

        target = self.canonical(params["path"]) if params.get("path") else ""
        event = {"name": name, "status": status, "target": target,
                 "mutating": not bool(getattr(tool, "read_only", True)),
                 "request_id": self.request_id, "id": call_id or uuid.uuid4().hex[:12],
                 "signature": action_signature(name, {**params, "path": target} if target else params)}
        if name == "write_file" and isinstance(params.get("content"), str):
            event["expected_sha256"] = hashlib.sha256(params["content"].encode("utf-8")).hexdigest()
        if status != "ok":
            detail = str(result)
            if re.search(r"permission|denied|not permitted|권한|거부", detail, re.I):
                event["status"] = "denied"
            elif re.search(r"timeout|timed out|unknown", detail, re.I):
                event["status"] = "unknown"
        self.events.append(event)
        if self.update is not None and not getattr(tool, "observability", False):
            self.update.analysis_complete = False
        coverage = getattr(result, "read_evidence", None)
        if status == "ok" and isinstance(tool, ReadFileTool) and isinstance(coverage, dict):
            self.evidence.append({**coverage, "id": uuid.uuid4().hex[:12],
                                  "request_id": self.request_id})
            self.evidence = self.evidence[-40:]
            actual_hash = (coverage.get("version") or {}).get("sha256")
            for previous in self.events:
                if (previous["target"] == coverage.get("target") and previous["status"] == "unknown"
                        and actual_hash and previous.get("expected_sha256") == actual_hash):
                    previous["status"] = "ok"
                    previous["verification"] = "read_after_unknown_write"
        self.persist("active")

    def covered(self, target: Target) -> bool:
        path = self.canonical(target.path)
        evidence = [e for e in self.evidence if e.get("target") == path and
                    (e.get("request_id") == self.request_id or self.valid_evidence(e))]
        for unit in ("lines", "pages"):
            ranges = [e for e in evidence if e.get("unit") == unit and e.get("extraction_ok")]
            if not ranges or (target.unit != "all" and target.unit != unit):
                continue
            # Never merge ranges from different file versions.
            versions = {json.dumps(e.get("version"), sort_keys=True) for e in ranges}
            for version in versions:
                group = [e for e in ranges if json.dumps(e.get("version"), sort_keys=True) == version]
                if not (group[0].get("version") or {}).get("sha256"):
                    # One returned range is observable; metadata alone cannot join snapshots.
                    if any(e["start"] <= (1 if target.unit == "all" else target.start)
                           and e["end"] >= (e["total"] if target.unit == "all" else target.end)
                           for e in group):
                        return True
                    continue
                total = group[-1].get("total")
                end = total if target.unit == "all" else target.end
                cursor = 1 if target.unit == "all" else target.start
                if end is None:
                    continue
                for e in sorted(group, key=lambda e: e["start"]):
                    if e["start"] > cursor:
                        break
                    cursor = max(cursor, e["end"] + 1)
                if cursor > end:
                    return True
        return False

    def defects(self) -> list[str]:
        update = self.update
        if update is None:
            return ["intent_unconfirmed"]
        defects = []
        instruction = re.split(r"```|\n(?:본문|제공된 내용)\s*[:：]", self.request, maxsplit=1)[0]
        explicit = _FILES.findall(instruction)
        def linked(path):
            return any(
                self.canonical(path) == self.canonical(t.path)
                or ("/" not in path and Path(t.path).name == path)
                or (Path(t.path).name in instruction and path in Path(t.path).name)
                or (t.path in instruction and path in t.path)
                for t in update.targets
            )
        if not all(linked(path) for path in explicit):
            defects.append("target_unresolved")
        if update.source == "files" and not update.targets:
            defects.append("target_unresolved")
        if update.source == "provided" and explicit:
            defects.append("target_unresolved")
        if (_FILE_REQUEST.search(instruction) and not update.targets
                and not (update.source == "provided" and _PROVIDED_REQUEST.search(instruction))):
            defects.append("target_unresolved")
        for target in update.targets:
            if target.action == "read" and not self.covered(target):
                defects.append(f"coverage_unconfirmed:{target.path}")
            if target.action == "write":
                events = [e for e in self.events if e["target"] == self.canonical(target.path)
                          and e["mutating"]]
                status = events[-1]["status"] if events else "missing"
                if status != "ok":
                    defects.append({"unknown": "record_check", "denied": "blocked"}.get(
                        status, "execution_required") + f":{target.path}")
                elif events[-1]["id"] not in update.result_refs:
                    defects.append(f"answer_correction:{target.path}:missing_result_reference")
        if _SAVE.search(instruction) and not _NO_WRITE.search(instruction) and not any(
                t.action == "write" for t in update.targets):
            defects.append("write_target_unresolved")
        if not update.analysis_complete:
            defects.append("analysis_incomplete")
        if update.status == "active":
            defects.append("completion_status_unconfirmed")
        if update.status == "completed" and not has_public_answer(update.final_answer):
            defects.append("answer_required")
        return list(dict.fromkeys(defects))

    def final_check(self) -> tuple[list[str], bool]:
        defects = self.defects()
        # Honest partial/blocked/question outcomes must stay incomplete without forcing retries.
        terminal = bool(self.update and self.update.status in {"partial", "blocked", "clarification"})
        return defects, terminal

    def incomplete(self, defects: list[str]) -> str:
        completed = [e["target"] or e["name"] for e in self.events if e["status"] == "ok"]
        return ("요청을 완료하지 못했습니다.\n"
                + ("확인된 실행: " + ", ".join(completed) + "\n" if completed else "")
                + "남은 확인: " + ", ".join(defects)
                + "\n성공했거나 결과가 불명확한 변경은 반복하지 않았습니다.")


class UpdateTaskTool(Tool):
    name = "update_task"
    description = "Record current intent, selected targets, requested scope and analysis status. Not execution approval."
    read_only = True
    observability = True
    concurrency_safe = False

    def __init__(self, note: TaskNote):
        self.note = note

    @property
    def parameters(self):
        return TaskUpdate.model_json_schema()

    async def execute(self, **kwargs):
        try:
            return self.note.accept(TaskUpdate.model_validate(kwargs))
        except ValueError as exc:
            return ToolResult.error(str(exc))


class TaskTools:
    """Turn-local overlay: never register a Genie tool in the shared registry."""
    def __init__(self, base: Any, note: TaskNote):
        self.base = base
        self.note = note
        self.control = UpdateTaskTool(note)

    def __getattr__(self, name):
        return getattr(self.base, name)

    def get(self, name):
        return self.control if name == "update_task" else self.base.get(name)

    def get_definitions(self):
        return [*self.base.get_definitions(), self.control.to_schema()]

    def prepare_call(self, name, params):
        if name == "update_task":
            return self.control, params, None
        return self.base.prepare_call(name, params)


async def bounded_operation(note: TaskNote, name: str, operation, *, timeout=None, grace=1.0):
    """Cancel and settle the actual child; late results stay in this run's audit only."""
    if timeout is None:
        timeout = {"find_files": 8.0, "grep": 12.0, "read_file": 20.0,
                   "web_search": 25.0, "web_fetch": 25.0}.get(name, 65.0)
    record = {"name": name, "request_id": note.request_id, "state": "running"}
    note.attempts.append(record)
    task = asyncio.create_task(operation())

    def late_result(done):
        record["late_result"] = "cancelled" if done.cancelled() else (
            "error" if done.exception() else "returned_after_cancellation")
        if not done.cancelled() and not done.exception():
            record["result"] = str(done.result())[:500]
        note.persist("cancelled" if record["state"] == "cancelled" else "incomplete")
        if note.persist_callback is not None:
            try:
                note.persist_callback()
            except Exception as exc:
                record["audit_persist_error"] = type(exc).__name__

    async def cancel_child():
        task.cancel()
        settled, _ = await asyncio.wait({task}, timeout=grace)
        record["cleanup"] = "settled" if settled else "isolated_running"
        task.add_done_callback(late_result)

    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            record["state"] = "unknown"
            await cancel_child()
            return ToolResult.error("Tool timeout; result unknown. Check target state before retry. "
                                    + record["cleanup"])
        result = task.result()
        record["state"] = "returned"
        return result
    except asyncio.CancelledError:
        record["state"] = "cancelled"
        await cancel_child()
        note.persist("cancelled")
        raise
    finally:
        if record["state"] != "cancelled":
            note.persist("active")

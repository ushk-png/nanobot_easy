"""Run one nanobot instance from a pinned, clean runtime checkout.

Every entry point (start/restart scripts, per-agent wrappers, direct gateway)
selects code through one profile file, never through the developer checkout:

    scripts/runtime.sh <profile.json> verify|gateway|up|restart|status|stop

A profile (see deploy/runtime-profile.example.json) names the runtime root,
the exact commit, the config, workspace, port and the per-agent invariants.
It must not contain secrets; env files are referenced by path only.

On `gateway` the running process writes a deployment record (no secrets)
with PID, process start time, commit, module paths/hashes, config hash,
workspace and port. `status` re-checks the record against the live process
so a stale file or a reused PID is reported instead of trusted.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

DEFAULT_MODULES = (
    "agent.context_selection", "agent.context_plan", "agent.time_allocation",
    "agent.runner", "agent.loop", "agent.context", "agent.memory_context",
    "agent.skills", "config.schema", "api.relay", "providers.base",
    "providers.xai_grok_provider", "evidence.packet",
)
TEMPLATES = ("templates/agent/tool_contract.md", "templates/agent/identity.md")


def fail(message: str) -> None:
    raise SystemExit(f"runtime launcher: {message}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(runtime: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(runtime), *args])


def process_start(pid: int) -> str | None:
    try:
        out = subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True)
    except subprocess.CalledProcessError:
        return None
    return " ".join(out.split()) or None


def load_profile(path: Path) -> dict:
    profile = json.loads(path.read_text(encoding="utf-8"))
    for key in ("instance", "runtime_root", "commit", "cwd", "config", "workspace", "port", "record"):
        if key not in profile:
            fail(f"profile {path} lacks {key!r}")
    return profile


def verify_checkout(profile: dict) -> dict:
    runtime = Path(profile["runtime_root"]).resolve()
    commit = profile["commit"]
    if os.environ.get("PYTHONPATH") != str(runtime):
        fail("PYTHONPATH must be exactly the runtime root (use scripts/runtime.sh)")
    if Path.cwd().resolve() != Path(profile["cwd"]).resolve():
        fail("cwd mismatch")
    if git(runtime, "rev-parse", "HEAD").decode().strip() != commit:
        fail("runtime checkout HEAD is not the pinned commit")
    if git(runtime, "status", "--porcelain", "--untracked-files=no").strip():
        fail("runtime checkout has product code changes")
    modules = {}
    for name in profile.get("modules", DEFAULT_MODULES):
        module = importlib.import_module("nanobot." + name)
        path = Path(module.__file__).resolve()
        relative = "nanobot/" + name.replace(".", "/") + ".py"
        committed = git(runtime, "show", f"{commit}:{relative}")
        if path != runtime / relative or path.read_bytes() != committed:
            fail(f"module path or bytes mismatch: {name}")
        modules[name] = {"path": str(path), "sha256": sha256(committed)}
    templates = {}
    for relative in TEMPLATES:
        committed = git(runtime, "show", f"{commit}:nanobot/{relative}")
        if (runtime / "nanobot" / relative).read_bytes() != committed:
            fail(f"template mismatch: {relative}")
        templates[relative] = sha256(committed)
    return {"modules": modules, "templates": templates}


def verify_config(profile: dict) -> dict:
    from nanobot.config.loader import load_config, set_config_path

    config_path = Path(profile["config"]).resolve()
    set_config_path(config_path)
    config = load_config(config_path)
    defaults = config.agents.defaults
    expect = profile.get("expect", {})
    if "on_demand_skills" in expect:
        if defaults.preload_always_skills or defaults.skills:
            fail("profile requires on-demand skill bodies (no preloaded skills)")
        if set(defaults.on_demand_skills) != set(expect["on_demand_skills"]):
            fail("on_demand_skills differ from profile")
    if expect.get("ordinary_conversation") and (
        defaults.context_selection.enabled or defaults.stepwise.enabled or defaults.stepwise.lightweight
    ):
        fail("profile requires the ordinary conversation path")
    if "time_allocation_contract_enabled" in expect and (
        bool(defaults.context_selection.time_allocation_contract_enabled)
        != bool(expect["time_allocation_contract_enabled"])
    ):
        fail("time_allocation_contract_enabled differs from profile")
    workspace = Path(defaults.workspace).expanduser().resolve()
    if workspace != Path(profile["workspace"]).resolve():
        fail("config workspace differs from profile workspace")
    return {
        "config": str(config_path),
        "config_sha256": sha256(config_path.read_bytes()),
        "workspace": str(workspace),
        "on_demand_skills": sorted(defaults.on_demand_skills),
        "context_selection_enabled": bool(defaults.context_selection.enabled),
        "time_allocation_contract_enabled": bool(
            defaults.context_selection.time_allocation_contract_enabled),
    }


def gateway_args(profile: dict) -> list[str]:
    return ["gateway", "--config", str(Path(profile["config"]).resolve()),
            "--workspace", str(Path(profile["workspace"]).resolve()),
            "--port", str(profile["port"]), "--foreground"]


def write_record(profile: dict, record: dict) -> None:
    target = Path(profile["record"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)


def record_status(profile: dict) -> dict:
    target = Path(profile["record"])
    if not target.exists():
        return {"record": "missing"}
    record = json.loads(target.read_text(encoding="utf-8"))
    pid = record.get("pid")
    live_start = process_start(pid) if isinstance(pid, int) else None
    if live_start is None:
        state = "stale_no_process"
    elif live_start != record.get("process_start"):
        state = "stale_pid_reused"
    elif record.get("commit") != profile["commit"]:
        state = "commit_differs_from_profile"
    else:
        state = "current"
    return {"record": state, "pid": pid, "process_start": live_start, "commit": record.get("commit")}


def control(profile: dict, profile_path: Path, action: str) -> None:
    from nanobot.cli.up import wait_for_health
    from nanobot.gateway import GatewayRuntime, GatewayRuntimePaths, GatewayStartOptions

    child = [sys.executable, "-B", str(Path(__file__).resolve()), str(profile_path), "gateway"]

    class PinnedRuntime(GatewayRuntime):
        def _build_child_command(self, options):  # noqa: ARG002 - fixed by profile
            return child

    workspace = str(Path(profile["workspace"]).resolve())
    config = str(Path(profile["config"]).resolve())
    manager = PinnedRuntime(paths=GatewayRuntimePaths.for_instance(
        data_dir=Path(profile.get("data_dir", Path(profile["cwd"]) / ".local")),
        workspace=workspace, config_path=config))
    current = manager.status()
    name = profile["instance"]
    if action == "status":
        print(json.dumps({name: {"running": current.running, "pid": current.pid,
                                 **record_status(profile)}}))
        raise SystemExit(0 if current.running else 1)
    if action == "stop":
        if current.running:
            manager.stop(timeout_s=int(profile.get("stop_timeout_s", 60)))
        stopped = not manager.status().running
        print(json.dumps({name: {"stopped": stopped}}))
        raise SystemExit(0 if stopped else 1)
    if action == "restart" and current.running:
        result = manager.stop(timeout_s=int(profile.get("stop_timeout_s", 60)))
        if not result.ok:
            fail(f"{name} stop failed: {result.message}")
        current = manager.status()
    if not current.running:
        result = manager.start_background(GatewayStartOptions(
            port=int(profile["port"]), workspace=workspace, config_path=config))
        if not result.ok:
            fail(f"{name} start failed: {result.message}")
    ready = wait_for_health(f"http://127.0.0.1:{profile['port']}/health",
                            is_alive=lambda: manager.status().running)
    active = manager.status()
    print(json.dumps({name: {"pid": active.pid, "running": active.running, "health_ready": ready,
                             **record_status(profile)}}))
    if not ready:
        fail(f"{name} health check did not become ready")


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[1] not in {"verify", "gateway", "up", "restart", "status", "stop"}:
        fail("usage: runtime_launcher.py <profile.json> verify|gateway|up|restart|status|stop")
    profile_path = Path(argv[0]).resolve()
    profile = load_profile(profile_path)
    action = argv[1]
    if action in {"up", "restart", "status", "stop"}:
        control(profile, profile_path, action)
        return
    checkout = verify_checkout(profile)
    config = verify_config(profile)
    record = {
        "instance": profile["instance"],
        "pid": os.getpid(),
        "process_start": process_start(os.getpid()),
        "commit": profile["commit"],
        "runtime_root": str(Path(profile["runtime_root"]).resolve()),
        "cwd": str(Path.cwd().resolve()),
        "port": int(profile["port"]),
        "profile": str(profile_path),
        "profile_sha256": sha256(profile_path.read_bytes()),
        "deployed_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "verification_only": action == "verify",
        **config,
        **checkout,
    }
    if action == "verify":
        print(json.dumps({"verified": record}, ensure_ascii=False))
        return
    write_record(profile, record)
    print(json.dumps({"runtime_record": profile["record"], "pid": record["pid"]}), flush=True)
    sys.argv = [sys.argv[0], *gateway_args(profile)]
    runpy.run_module("nanobot", run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])

"""Offline checks for the pinned runtime launcher (no gateway is started)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_launcher", ROOT / "scripts/runtime_launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def _profile(tmp_path: Path, **extra) -> dict:
    data = {"instance": "t", "runtime_root": str(tmp_path), "commit": "c" * 40,
            "cwd": str(tmp_path), "config": str(tmp_path / "config.json"),
            "workspace": str(tmp_path / "ws"), "port": 1, "record": str(tmp_path / "rec.json")}
    data.update(extra)
    return data


def test_profile_requires_core_fields(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"instance": "t"}))
    with pytest.raises(SystemExit):
        launcher.load_profile(path)


def test_record_status_detects_missing_stale_and_current(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    assert launcher.record_status(profile) == {"record": "missing"}
    me = os.getpid()
    rec = {"pid": me, "process_start": launcher.process_start(me), "commit": "c" * 40}
    Path(profile["record"]).write_text(json.dumps(rec))
    assert launcher.record_status(profile)["record"] == "current"
    rec["process_start"] = "Thu Jan  1 00:00:00 1970"
    Path(profile["record"]).write_text(json.dumps(rec))
    assert launcher.record_status(profile)["record"] == "stale_pid_reused"
    rec["commit"] = "d" * 40
    rec["process_start"] = launcher.process_start(me)
    Path(profile["record"]).write_text(json.dumps(rec))
    assert launcher.record_status(profile)["record"] == "commit_differs_from_profile"


def test_gateway_workspace_preserves_skill_identity_and_approval(tmp_path: Path, monkeypatch) -> None:
    from nanobot.skill_store import SkillStore

    workspace = tmp_path / "ws"
    skill = workspace / "skills" / "custom" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: custom\ndescription: local test\n---\nTest method.\n")
    empty_builtins = tmp_path / "empty-builtins"
    empty_builtins.mkdir()
    store = SkillStore(workspace)
    store.reindex(builtin_dir=empty_builtins)
    store.approve_draft("custom")
    store.record_skill_outcome("custom", gate_result="ok")
    before = dict(store.get_skill("custom"))

    monkeypatch.chdir(tmp_path)
    # A config can contain a relative workspace; the profile's verified absolute
    # workspace must reach the gateway instead of deriving different skill IDs.
    args = launcher.gateway_args(_profile(tmp_path))
    child_workspace = Path(args[args.index("--workspace") + 1])
    assert child_workspace.is_absolute()
    child = SkillStore(child_workspace)
    child.reindex(builtin_dir=empty_builtins)
    after = dict(child.get_skill("custom"))
    for key in ("id", "status", "usage_count", "success_count"):
        assert after[key] == before[key]


def _select(script: Path, config: Path, workspace: Path, profiles: Path, env: dict) -> subprocess.CompletedProcess:
    # Extract select_runtime_profile from the script and call it in isolation.
    text = script.read_text()
    start = text.index("select_runtime_profile() {")
    end = text.index("\nPY\n}\n", start) + len("\nPY\n}\n")
    body = text[start:end] + f'select_runtime_profile "{config}" "{workspace}" "{profiles}"\n'
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)


@pytest.mark.parametrize("script", ["start-nanobot-easy.sh", "scripts/restart-nanobot-easy.sh"])
def test_start_and_restart_select_the_same_profile(tmp_path: Path, script: str) -> None:
    profiles = tmp_path / "runtimes"
    profiles.mkdir()
    (profiles / "a.json").write_text(json.dumps(_profile(tmp_path)))
    (profiles / "b.json").write_text(json.dumps(_profile(tmp_path, config=str(tmp_path / "other.json"))))
    env = {k: v for k, v in os.environ.items() if k != "NANOBOT_RUNTIME_PROFILE"}
    hit = _select(ROOT / script, tmp_path / "config.json", tmp_path / "ws", profiles, env)
    assert hit.stdout.strip() == str((profiles / "a.json").resolve())
    miss = _select(ROOT / script, tmp_path / "none.json", tmp_path / "ws", profiles, env)
    assert miss.returncode == 0 and miss.stdout.strip() == ""
    env["NANOBOT_RUNTIME_PROFILE"] = str(profiles / "b.json")
    bad = _select(ROOT / script, tmp_path / "config.json", tmp_path / "ws", profiles, env)
    assert bad.returncode != 0

"""verify_routing must not touch a real instance unless told to (review C5-04)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def _module():
    sys.modules.pop("scripts.verify_routing", None)
    return importlib.import_module("scripts.verify_routing")


class _RecordingStore:
    seen: list[Path] = []

    def __init__(self, workspace: Path) -> None:
        type(self).seen.append(Path(workspace))

    def ensure_index(self, **_kwargs) -> None:
        return None

    def list_skills(self, **_kwargs) -> list:
        return []


def test_default_workspace_is_a_fresh_temporary_directory(monkeypatch, tmp_path, capsys) -> None:
    module = _module()
    _RecordingStore.seen = []
    monkeypatch.setattr(module, "SkillStore", _RecordingStore)
    monkeypatch.setattr(sys, "argv", ["verify_routing.py"])
    monkeypatch.chdir(tmp_path)
    module.main()
    (workspace,) = _RecordingStore.seen
    assert workspace != Path(".local/workspace") and ".local" not in workspace.parts
    assert "workspace=" in capsys.readouterr().err


def test_explicit_workspace_is_used_as_given(monkeypatch, tmp_path) -> None:
    module = _module()
    _RecordingStore.seen = []
    monkeypatch.setattr(module, "SkillStore", _RecordingStore)
    monkeypatch.setattr(sys, "argv", ["verify_routing.py", "--workspace", str(tmp_path / "ws")])
    module.main()
    assert _RecordingStore.seen == [tmp_path / "ws"]


def test_config_with_embeddings_needs_an_explicit_cost_flag(monkeypatch, tmp_path, capsys) -> None:
    module = _module()
    _RecordingStore.seen = []
    monkeypatch.setattr(module, "SkillStore", _RecordingStore)
    monkeypatch.setattr(module, "load_config", lambda _path: object())
    monkeypatch.setattr(module, "make_skill_embedding_fn", lambda _config: (lambda *_: [], "model", 8))
    monkeypatch.setattr(sys, "argv", ["verify_routing.py", "--config", str(tmp_path / "config.json")])
    assert module.main() == 2
    assert _RecordingStore.seen == []  # nothing opened, nothing re-indexed
    assert "--allow-embedding-cost" in capsys.readouterr().err

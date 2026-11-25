"""A read error or a stray non-object line must never turn a session into an empty one (C2-11)."""

from __future__ import annotations

import builtins
import errno
import json

import pytest

from nanobot.session.manager import SessionManager


def _seed(tmp_path, key: str = "telegram:1", count: int = 10) -> SessionManager:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create(key)
    for index in range(count):
        session.add_message("user" if index % 2 == 0 else "assistant", f"message {index}")
    manager.save(session)
    return SessionManager(tmp_path)  # a restarted process: empty cache


def _file_message_count(manager: SessionManager, key: str) -> int:
    path = manager._get_session_path(key)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return sum(1 for item in lines if item.get("_type") != "metadata")


def test_transient_read_error_is_retried_then_succeeds(tmp_path, monkeypatch) -> None:
    manager = _seed(tmp_path)
    real_open, failures = builtins.open, {"left": 2}

    def flaky(path, *args, **kwargs):
        if str(path).endswith(".jsonl") and failures["left"] and "w" not in (args[0] if args else ""):
            failures["left"] -= 1
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky)
    session = manager.get_or_create("telegram:1")
    monkeypatch.setattr(builtins, "open", real_open)
    assert len(session.messages) == 10


def test_persistent_read_error_raises_and_leaves_the_file_alone(tmp_path, monkeypatch) -> None:
    manager = _seed(tmp_path)
    real_open = builtins.open

    def broken(path, *args, **kwargs):
        if str(path).endswith(".jsonl") and "w" not in (args[0] if args else ""):
            raise OSError(errno.EIO, "I/O error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", broken)
    with pytest.raises(OSError):
        manager.get_or_create("telegram:1")
    monkeypatch.setattr(builtins, "open", real_open)
    assert _file_message_count(manager, "telegram:1") == 10
    assert len(SessionManager(tmp_path).get_or_create("telegram:1").messages) == 10


@pytest.mark.parametrize("stray", ["null", "7", "[1, 2]", '"text"'])
def test_non_object_line_is_skipped_not_fatal(tmp_path, stray) -> None:
    manager = _seed(tmp_path)
    path = manager._get_session_path("telegram:1")
    with path.open("a", encoding="utf-8") as f:
        f.write(stray + "\n")
    session = manager.get_or_create("telegram:1")
    assert len(session.messages) == 10
    manager.save(session)
    assert _file_message_count(manager, "telegram:1") == 10


def test_file_with_nothing_readable_is_preserved_before_a_new_session_saves(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    path = manager._get_session_path("telegram:2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\nnull\n", encoding="utf-8")
    session = manager.get_or_create("telegram:2")
    assert session.messages == []
    manager.save(session)
    kept = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "not json\nnull\n"

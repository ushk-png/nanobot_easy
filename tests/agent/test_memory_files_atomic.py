"""Durable memory files are replaced atomically, never half written (review N02, N13)."""

from __future__ import annotations

import os

import pytest

from nanobot.agent.memory import MemoryStore


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path)


@pytest.mark.parametrize("writer,reader", [
    ("write_memory", "read_memory"), ("write_soul", "read_soul"), ("write_user", "read_user"),
])
def test_a_failed_write_leaves_the_old_content_and_no_temp_file(tmp_path, monkeypatch, writer, reader) -> None:
    store = _store(tmp_path)
    getattr(store, writer)("old content")
    real_fsync = os.fsync

    def failing(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", failing)
    with pytest.raises(OSError):
        getattr(store, writer)("new content that never fully lands")
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert getattr(store, reader)() == "old content"
    assert not [p for p in tmp_path.rglob("*.tmp")]


def test_cursor_files_are_replaced_atomically(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.set_last_dream_cursor(7)
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "I/O error")))
    with pytest.raises(OSError):
        store.set_last_dream_cursor(9)
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert store.get_last_dream_cursor() == 7

"""Item 6: a git workspace must not re-send the same Dream batch forever.

A completed run that changes no memory file keeps the cursor (the audit trail is the real
diff), but after two such runs in a row on the same batch the cursor advances.
"""
from __future__ import annotations

import json

import pytest

from nanobot.agent.memory import MemoryStore


@pytest.fixture
def git_store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "ws", max_history_entries=10)
    assert store.git.init()
    assert store.git.is_initialized()
    return store


@pytest.fixture
def plain_store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "plain", max_history_entries=10)
    assert not store.git.is_initialized()
    return store


def test_git_completed_no_change_advances_on_the_second_run(git_store: MemoryStore) -> None:
    assert git_store.dream_cursor_should_advance("", True, 20) is False
    assert git_store.dream_cursor_should_advance("", True, 20) is True


def test_git_real_change_advances_at_once(git_store: MemoryStore) -> None:
    assert git_store.dream_cursor_should_advance("MEMORY.md: +1 -0", True, 20) is True
    assert git_store.dream_cursor_should_advance("MEMORY.md: +1 -0", False, 40) is True


def test_git_incomplete_runs_never_advance_or_count(git_store: MemoryStore) -> None:
    for _ in range(5):
        assert git_store.dream_cursor_should_advance("", False, 20) is False
    # The streak did not grow, so one completed no-change run still is not enough.
    assert git_store.dream_cursor_should_advance("", True, 20) is False
    assert git_store.dream_cursor_should_advance("", True, 20) is True


def test_git_streak_restarts_when_the_batch_changes(git_store: MemoryStore) -> None:
    assert git_store.dream_cursor_should_advance("", True, 20) is False
    assert git_store.dream_cursor_should_advance("", True, 25) is False
    assert git_store.dream_cursor_should_advance("", True, 25) is True


def test_git_change_resets_the_streak(git_store: MemoryStore) -> None:
    assert git_store.dream_cursor_should_advance("", True, 20) is False
    assert git_store.dream_cursor_should_advance("SOUL.md: +1 -0", True, 20) is True
    assert git_store.dream_cursor_should_advance("", True, 20) is False


def test_non_git_completed_advances_once(plain_store: MemoryStore) -> None:
    assert plain_store.dream_cursor_should_advance("", True, 20) is True
    assert plain_store.dream_cursor_should_advance("", False, 20) is False


def test_missing_or_broken_state_reads_as_zero(git_store: MemoryStore) -> None:
    state = git_store.memory_dir / ".dream_state.json"
    state.write_text("not json", encoding="utf-8")
    assert git_store.dream_cursor_should_advance("", True, 20) is False
    state.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert git_store.dream_cursor_should_advance("", True, 20) is False
    assert git_store.dream_cursor_should_advance("", True, 20) is True


def test_dream_cursor_file_format_is_unchanged(git_store: MemoryStore) -> None:
    git_store.dream_cursor_should_advance("", True, 20)
    git_store.set_last_dream_cursor(20)
    assert (git_store.memory_dir / ".dream_cursor").read_text(encoding="utf-8") == "20"


def test_no_change_batches_no_longer_starve_newer_history(git_store: MemoryStore) -> None:
    """The r06 scenario: 60 entries, cap 10, every run completes with no edits."""
    for i in range(60):
        git_store.append_history(f"synthetic entry {i}")
    seen_batches = []
    for _ in range(6):
        result = git_store.build_dream_prompt()
        if result is None:
            break
        _prompt, last = result
        seen_batches.append(last)
        if git_store.dream_cursor_should_advance("", True, last):
            git_store.set_last_dream_cursor(last)
        git_store.compact_history()
    assert git_store.get_last_dream_cursor() >= 60
    assert len(set(seen_batches)) == 3
    assert len(git_store._read_entries()) <= 10

"""compact_history never drops history the Dream cursor has not passed (and still trims processed history)."""
from __future__ import annotations

import json

from nanobot.agent.memory import MemoryStore


def make(tmp_path, n, keep=3, dream_cursor=None):
    store = MemoryStore(tmp_path, max_history_entries=keep)
    for i in range(1, n + 1):
        store.append_history(f"event {i}")
    if dream_cursor is not None:
        store.set_last_dream_cursor(dream_cursor)
    return store


def cursors(store):
    return [e["cursor"] for e in store._read_entries()]


def test_unprocessed_entries_outside_the_recent_window_are_kept(tmp_path):
    store = make(tmp_path, 10, keep=3, dream_cursor=4)
    store.compact_history()
    assert cursors(store) == [5, 6, 7, 8, 9, 10]            # 5..7 are older than the window but unprocessed


def test_processed_old_entries_are_still_trimmed(tmp_path):
    store = make(tmp_path, 10, keep=3, dream_cursor=10)
    store.compact_history()
    assert cursors(store) == [8, 9, 10]                      # the existing retention policy


def test_a_partly_processed_backlog_keeps_only_the_unprocessed_part_plus_the_window(tmp_path):
    store = make(tmp_path, 12, keep=3, dream_cursor=6)
    store.compact_history()
    assert cursors(store) == [7, 8, 9, 10, 11, 12]


def test_a_large_backlog_may_exceed_the_limit(tmp_path):
    store = make(tmp_path, 30, keep=3, dream_cursor=0)
    store.compact_history()
    assert cursors(store) == list(range(1, 31))              # 30 > 3 and that is intended


def test_no_dream_cursor_file_means_everything_is_backlog(tmp_path):
    store = make(tmp_path, 8, keep=2)
    assert not store._dream_cursor_file.exists()
    store.compact_history()
    assert cursors(store) == list(range(1, 9))
    assert not store._dream_cursor_file.exists()             # compaction never moves or creates the cursor


def test_order_content_and_cursors_are_unchanged_and_nothing_is_duplicated(tmp_path):
    store = make(tmp_path, 9, keep=3, dream_cursor=2)
    before = {e["cursor"]: e for e in store._read_entries()}
    store.compact_history()
    after = store._read_entries()
    assert cursors(store) == sorted(set(cursors(store))) == list(range(3, 10))
    assert all(before[e["cursor"]] == e for e in after)
    assert store.get_last_dream_cursor() == 2


def test_the_boundary_entry_just_after_the_cursor_is_kept_and_the_one_at_it_is_not(tmp_path):
    store = make(tmp_path, 8, keep=2, dream_cursor=5)
    store.compact_history()
    assert cursors(store) == [6, 7, 8]                       # 5 is processed, 6 is the first unprocessed


def test_repeated_compaction_loses_nothing_and_stops_rewriting(tmp_path, monkeypatch):
    store = make(tmp_path, 10, keep=3, dream_cursor=4)
    store.compact_history()
    first = cursors(store)
    writes = []
    original = store._write_entries
    monkeypatch.setattr(store, "_write_entries", lambda entries: (writes.append(1), original(entries))[1])
    store.compact_history()
    store.compact_history()
    assert cursors(store) == first and writes == []
    store.append_history("event 11")
    store.set_last_dream_cursor(8)
    store.compact_history()
    assert cursors(store) == [9, 10, 11] and writes == [1]   # later progress trims, still without losing 9+


def test_nothing_to_drop_means_no_rewrite(tmp_path, monkeypatch):
    store = make(tmp_path, 10, keep=3, dream_cursor=0)       # all backlog
    writes = []
    monkeypatch.setattr(store, "_write_entries", lambda entries: writes.append(entries))
    store.compact_history()
    assert writes == []
    small = make(tmp_path / "small", 2, keep=5, dream_cursor=2)
    monkeypatch.setattr(small, "_write_entries", lambda entries: writes.append(entries))
    small.compact_history()
    assert writes == []


def test_unlimited_retention_setting_is_unchanged(tmp_path):
    store = make(tmp_path, 6, keep=0, dream_cursor=6)
    store.compact_history()
    assert cursors(store) == list(range(1, 7))


def test_entries_the_dream_reader_would_skip_are_not_kept_as_backlog(tmp_path):
    store = make(tmp_path, 6, keep=2, dream_cursor=0)
    with open(store.history_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"cursor": "x", "timestamp": "t", "content": "bad cursor"}) + "\n")
        handle.write(json.dumps({"cursor": 99, "timestamp": 1, "content": "bad payload"}) + "\n")
        handle.write("not json at all\n")
        handle.write(json.dumps({"cursor": 7, "timestamp": "t", "content": "recent a"}) + "\n")
        handle.write(json.dumps({"cursor": 8, "timestamp": "t", "content": "recent b"}) + "\n")
    store.compact_history()
    kept = [e["content"] for e in store._read_entries()]
    assert [c for c in kept if c.startswith("event")] == [f"event {i}" for i in range(1, 7)]
    assert "bad cursor" not in kept and "bad payload" not in kept and kept[-2:] == ["recent a", "recent b"]


def test_the_write_stays_atomic(tmp_path, monkeypatch):
    import os

    store = make(tmp_path, 6, keep=2, dream_cursor=6)
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real_replace(a, b))[1])
    store.compact_history()
    assert replaced and replaced[0][0].endswith(".tmp") and replaced[0][1] == str(store.history_file)
    assert cursors(store) == [5, 6]

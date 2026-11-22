"""Phase 4: MEMORY.md sections in the event store (synthetic data, temp workspace/DB only)."""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import threading
from pathlib import Path

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.memory import (
    ForgetMemoryEventsTool,
    ReadMemoryEventsTool,
    SearchMemoryTool,
)
from nanobot.memory import curated_source
from nanobot.memory.curated_source import split_sections, sync_memory_md
from nanobot.memory.event_store import CHUNK_CHARS, DOC_OWNER, ConversationEventStore
from nanobot.memory.models import MemoryScope

DOC = """# Preferences
- Prefers synthetic-oolong tea in the morning
- Editor of choice is fake-vim-9000

# Project Kestrel
Kestrel launch window is the third Tuesday.
Owner of Kestrel is the synthetic team Heron.
"""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*_a, **_k):
        raise RuntimeError("network blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _ws(tmp_path: Path, text: str | None = DOC, name: str = "ws") -> Path:
    ws = tmp_path / name
    (ws / "memory").mkdir(parents=True)
    if text is not None:
        (ws / "memory" / "MEMORY.md").write_text(text, encoding="utf-8")
    return ws


def _tool(cls, ws: Path, chat: str = "chat-a", agent: str | None = None):
    tool = cls(str(ws))
    meta = {"agent_id": agent} if agent else {}
    tool.set_context(RequestContext(channel="cli", chat_id=chat, session_key=f"cli:{chat}", metadata=meta))
    return tool


def run(coro):
    return asyncio.run(coro)


def _edit(path: Path, text: str) -> None:
    before = path.stat().st_mtime_ns
    path.write_text(text, encoding="utf-8")
    if path.stat().st_mtime_ns == before:  # coarse clocks: force a visible change
        os.utime(path, ns=(before + 1_000_000, before + 1_000_000))


def _doc_rows(ws: Path, where: str = "1=1") -> list[sqlite3.Row]:
    conn = sqlite3.connect(ws / "memory" / "conversation_memory.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT * FROM events WHERE owner_id = ? AND {where} ORDER BY sequence",
                            (DOC_OWNER,)).fetchall()
    finally:
        conn.close()


def _ids(output: str) -> list[str]:
    return [line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("event_id: ")]


def test_sectioning_is_lossless_with_exact_spans():
    text = "intro line\n\n# H1\n- a\n  continued\n- b\n\npara one\npara two\n## H2\n1. x\n"
    sections = split_sections(text)
    lines = text.splitlines()
    covered = [n for s in sections for n in range(s.line_start, s.line_end + 1)]
    assert len(covered) == len(set(covered))
    assert {n for n, line in enumerate(lines, 1) if line.strip()} <= set(covered)
    for s in sections:
        assert s.text == "\n".join(lines[s.line_start - 1:s.line_end])
    assert [s.heading for s in sections] == ["", "H1", "H1", "H1", "H2"]


def test_first_registration_search_and_raw_read(tmp_path):
    ws = _ws(tmp_path)
    out = run(_tool(SearchMemoryTool, ws).execute(query="synthetic-oolong"))
    ids = _ids(out)
    assert ids and ids[0].startswith("memmd-")
    assert "source: memory/MEMORY.md lines 1-2" in out and "version: current" in out
    assert "not a conversation transcript" in out
    raw = run(_tool(ReadMemoryEventsTool, ws).execute(event_ids=ids[:1]))
    assert "# Preferences\n- Prefers synthetic-oolong tea in the morning" in raw
    assert "lines 1-2" in raw and "conversation" in raw
    assert len(_doc_rows(ws)) == 3  # 2 bullets (first carries the heading) + 1 paragraph section
    assert all(r["actor"] == "memory_md" and r["agent_id"] is None for r in _doc_rows(ws))


def test_edit_replaces_current_and_keeps_past_readable(tmp_path):
    ws = _ws(tmp_path)
    first = _ids(run(_tool(SearchMemoryTool, ws).execute(query="third Tuesday")))
    md = ws / "memory" / "MEMORY.md"
    _edit(md, DOC.replace("third Tuesday", "first Friday"))
    search = _tool(SearchMemoryTool, ws)
    assert first[0] not in run(search.execute(query="third Tuesday"))
    new = run(search.execute(query="first Friday"))
    assert _ids(new) and _ids(new)[0] != first[0]
    # unchanged sections keep their event IDs
    oolong = [r["event_id"] for r in _doc_rows(ws, "content LIKE '%oolong%'")]
    assert len(oolong) == 1 and _doc_rows(ws, "content LIKE '%oolong%'")[0]["superseded_at"] is None
    past = run(_tool(ReadMemoryEventsTool, ws).execute(event_ids=first[:1]))
    assert "version: superseded" in past and "third Tuesday" in past
    # changing it back restores the original ID instead of duplicating it
    _edit(md, DOC)
    assert first[0] in run(search.execute(query="third Tuesday"))
    assert len(_doc_rows(ws)) == 4


def test_empty_missing_and_read_error_are_distinct(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    md = ws / "memory" / "MEMORY.md"
    store = ConversationEventStore(ws)
    wid = str(ws.resolve())
    assert sync_memory_md(store, wid).state == "fresh"

    original = Path.read_bytes

    def denied(self):
        if self.name == "MEMORY.md":
            raise PermissionError("synthetic")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    _edit(md, DOC + "- extra synthetic-ghost line\n")
    status = sync_memory_md(store, wid)
    assert status.state == "unverified" and "PermissionError" in status.error
    assert len(_doc_rows(ws, "superseded_at IS NULL")) == 3  # not treated as deletion
    out = run(_tool(SearchMemoryTool, ws).execute(query="synthetic-oolong"))
    assert "UNVERIFIED" in out and "memmd-" not in out  # stale rows are not presented as current
    monkeypatch.setattr(Path, "read_bytes", original)

    _edit(md, "")
    assert sync_memory_md(store, wid).state == "empty"
    assert not _doc_rows(ws, "superseded_at IS NULL")
    _edit(md, DOC)
    assert sync_memory_md(store, wid).state == "fresh"
    md.unlink()
    status = sync_memory_md(store, wid)
    assert status.state == "missing" and not _doc_rows(ws, "superseded_at IS NULL")
    out = run(_tool(SearchMemoryTool, ws).execute(query="synthetic-oolong"))
    assert "file not found" in out and "memmd-" not in out
    assert md.exists() is False  # sync never recreates or edits the source


def test_long_paragraph_and_long_line_are_not_lost(tmp_path):
    long_line = "x" * (CHUNK_CHARS * 2 + 123) + " tail-marker-omega"
    para = "\n".join(f"line {i} " + "y" * 300 for i in range(40)) + "\nparagraph-end-marker-sigma"
    ws = _ws(tmp_path, f"# Long\n{long_line}\n\n{para}\n")
    search = _tool(SearchMemoryTool, ws)
    for marker in ("tail-marker-omega", "paragraph-end-marker-sigma"):
        ids = _ids(run(search.execute(query=marker)))
        assert ids, marker
        raw = run(_tool(ReadMemoryEventsTool, ws).execute(event_ids=ids[:1]))
        assert marker in raw
    conn = sqlite3.connect(ws / "memory" / "conversation_memory.db")
    chunks = [r[0] for r in conn.execute("SELECT text FROM event_chunks")]
    assert any("tail-marker-omega" in c for c in chunks) and any("paragraph-end-marker-sigma" in c for c in chunks)
    assert all(len(c) <= CHUNK_CHARS for c in chunks)


def test_repeated_sync_and_touch_do_not_write_or_duplicate(tmp_path):
    ws = _ws(tmp_path)
    store = ConversationEventStore(ws)
    wid = str(ws.resolve())
    assert sync_memory_md(store, wid).changed
    db = ws / "memory" / "conversation_memory.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    for _ in range(5):
        assert not sync_memory_md(store, wid).changed
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before  # no DB write when unchanged
    md = ws / "memory" / "MEMORY.md"
    st = md.stat()
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert not sync_memory_md(store, wid).changed
    assert len(_doc_rows(ws)) == 3


def test_scopes_single_copy_per_workspace_and_no_widening(tmp_path):
    ws = _ws(tmp_path)
    other = _ws(tmp_path, DOC.replace("oolong", "rooibos"), name="ws2")
    scope_a = MemoryScope.from_runtime(owner_id="chat-a", workspace_id=str(ws.resolve()), agent_id=None)
    store = ConversationEventStore(ws)
    private = store.append_event(scope=scope_a, conversation_id="cli:chat-a", session_id="cli:chat-a",
                                 actor="user", event_type="USER_MESSAGE", content="private synthetic-oolong note")
    out_a = run(_tool(SearchMemoryTool, ws, "chat-a").execute(query="synthetic-oolong", limit=10))
    out_b = run(_tool(SearchMemoryTool, ws, "chat-b", agent="agent-x").execute(
        query="synthetic-oolong", limit=10, owner_id="chat-a", workspace_id="*"))  # extra args ignored
    assert private.event_id in out_a and private.event_id not in out_b
    doc_ids = [r["event_id"] for r in _doc_rows(ws, "content LIKE '%oolong%'")]
    assert doc_ids[0] in out_a and doc_ids[0] in out_b
    assert len(_doc_rows(ws)) == 3  # shared once, not copied per user
    assert all(r["owner_id"] != "local" for r in _doc_rows(ws))
    assert doc_ids[0] not in run(_tool(SearchMemoryTool, other).execute(query="synthetic-oolong"))
    assert not run(_tool(ReadMemoryEventsTool, other).execute(event_ids=doc_ids)).count("synthetic-oolong")
    # default (non-tool) callers keep the old owner-only behaviour
    from nanobot.memory.search import MemorySearcher

    default = MemorySearcher(store).search(scope=scope_a, query="synthetic-oolong")
    assert all(not eid.startswith("memmd-") for w in default.windows for eid in w.event_ids)


@pytest.mark.parametrize("mode", ["forget", "purge"])
def test_forget_and_purge_survive_resync_restart_and_reindex(tmp_path, mode):
    ws = _ws(tmp_path)
    target = _ids(run(_tool(SearchMemoryTool, ws).execute(query="fake-vim-9000")))[0]
    # Trusted store-level purge remains available; the runtime tool uses personal hiding
    # for shared sections rather than granting every chat a workspace-wide purge.
    scope = MemoryScope.from_runtime(owner_id="chat-a", workspace_id=str(ws.resolve()), agent_id=None)
    assert ConversationEventStore(ws).forget_events(
        scope, [target], purge=mode == "purge", include_documents=True) == 1
    md = ws / "memory" / "MEMORY.md"
    assert "fake-vim-9000" in md.read_text()  # source file untouched
    _edit(md, DOC + "\n# Extra\n- unrelated synthetic edit\n")
    ConversationEventStore(ws).rebuild_derived_indexes()  # reindex + "restart" (fresh instances)
    _edit(md, DOC.replace("Heron", "Egret"))
    for _ in range(2):
        out = run(_tool(SearchMemoryTool, ws).execute(query="fake-vim-9000"))
        assert target not in _ids(out)
        assert "fake-vim-9000" not in out.split("query: fake-vim-9000", 1)[1]
    assert len(_doc_rows(ws, "event_id = '%s'" % target)) == 1  # minimal metadata row only
    raw = run(_tool(ReadMemoryEventsTool, ws).execute(event_ids=[target]))
    assert "fake-vim-9000" not in raw
    if mode == "purge":
        row = _doc_rows(ws, "event_id = '%s'" % target)[0]
        assert row["content"] is None and row["metadata_json"] is None
        conn = sqlite3.connect(ws / "memory" / "conversation_memory.db")
        conn.execute("VACUUM")
        conn.close()
        assert b"fake-vim-9000" not in (ws / "memory" / "conversation_memory.db").read_bytes()
    tomb = sqlite3.connect(ws / "memory" / "conversation_memory.db").execute(
        "SELECT * FROM memory_doc_tombstones").fetchall()
    assert len(tomb) == 1 and "fake-vim" not in repr(tomb)


def test_db_lock_degrades_to_unverified_without_blocking(tmp_path):
    ws = _ws(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="chat-a", workspace_id=str(ws.resolve()), agent_id=None)
    event = ConversationEventStore(ws).append_event(
        scope=scope, conversation_id="cli:chat-a", session_id="cli:chat-a", actor="assistant",
        event_type="ASSISTANT_MESSAGE", content="conversation mentions synthetic-oolong")
    _edit(ws / "memory" / "MEMORY.md", DOC + "- new synthetic line\n")
    locker = sqlite3.connect(ws / "memory" / "conversation_memory.db", isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        out = run(_tool(SearchMemoryTool, ws).execute(query="synthetic-oolong"))
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert event.event_id in out and "UNVERIFIED" in out and "memmd-" not in out
    assert "memmd-" in run(_tool(SearchMemoryTool, ws).execute(query="synthetic-oolong"))


def test_legacy_db_migrates_additively_and_keeps_ids(tmp_path):
    ws = _ws(tmp_path, None)
    store = ConversationEventStore(ws)
    scope = MemoryScope.from_runtime(owner_id="chat-a", workspace_id=str(ws.resolve()), agent_id=None)
    e = store.append_event(scope=scope, conversation_id="c", session_id="cli:chat-a", actor="user",
                           event_type="USER_MESSAGE", content="legacy synthetic-pelican question")
    conn = sqlite3.connect(store.db_path)
    conn.executescript("DROP TABLE memory_doc_sources; DROP TABLE memory_doc_tombstones;"
                       "ALTER TABLE events DROP COLUMN superseded_at;")
    conn.close()
    store = ConversationEventStore(ws)  # reopen = migration
    cols = [r[1] for r in sqlite3.connect(store.db_path).execute("PRAGMA table_info(events)")]
    assert "superseded_at" in cols
    assert [x.event_id for x in store.get_events(scope, [e.event_id])] == [e.event_id]
    out = run(_tool(SearchMemoryTool, ws).execute(query="synthetic-pelican"))
    assert e.event_id in out and "file not found" in out
    assert store.get_events(scope, [e.event_id])[0].content == "legacy synthetic-pelican question"


def test_tool_schema_and_prompt_inputs_unchanged(tmp_path):
    ws = _ws(tmp_path)
    tools = [_tool(c, ws) for c in (SearchMemoryTool, ReadMemoryEventsTool, ForgetMemoryEventsTool)]
    assert "include_documents" not in repr([t.parameters for t in tools])
    assert all("MEMORY.md" not in t.description for t in tools)


def test_file_change_during_sync_is_not_mixed(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    md = ws / "memory" / "MEMORY.md"
    store = ConversationEventStore(ws)
    wid = str(ws.resolve())
    sync_memory_md(store, wid)
    _edit(md, DOC.replace("Heron", "Egret"))
    original = Path.read_bytes

    def racing(self):
        data = original(self)
        if self.name == "MEMORY.md":
            monkeypatch.setattr(Path, "read_bytes", original)
            _edit(md, DOC.replace("Heron", "Ibis"))  # concurrent writer after our read
        return data

    monkeypatch.setattr(Path, "read_bytes", racing)
    status = sync_memory_md(store, wid)
    assert status.state == "unverified" and "changed during sync" in status.error
    current = [r["content"] for r in _doc_rows(ws, "superseded_at IS NULL")]
    assert any("Heron" in c for c in current) and not any("Egret" in c or "Ibis" in c for c in current)
    assert sync_memory_md(store, wid).state == "fresh"
    current = [r["content"] for r in _doc_rows(ws, "superseded_at IS NULL")]
    assert any("Ibis" in c for c in current) and not any("Heron" in c or "Egret" in c for c in current)


def test_overlapping_syncs_converge_without_duplicates(tmp_path):
    ws = _ws(tmp_path)
    wid = str(ws.resolve())
    results = []

    def worker():
        results.append(sync_memory_md(ConversationEventStore(ws), wid, lock_timeout=5.0))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.state == "fresh" for r in results)
    assert len(_doc_rows(ws)) == 3
    assert curated_source.PAST_VERSION_CAP > 0

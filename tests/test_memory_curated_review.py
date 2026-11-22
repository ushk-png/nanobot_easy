"""Review regressions for derived memory; synthetic files only, no model calls."""

import asyncio
import threading

import pytest
from test_memory_curated_source import DOC, _doc_rows, _edit, _ids, _tool, _ws

from nanobot.agent.context_evidence import active_evidence_capture
from nanobot.agent.tools.memory import (
    ForgetMemoryEventsTool,
    ReadMemoryEventsTool,
    SearchMemoryTool,
)
from nanobot.memory import curated_source
from nanobot.memory.curated_source import DocSyncStatus, sync_memory_md
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope


async def test_sync_wait_does_not_block_the_event_loop(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    gate = threading.Event()
    original = curated_source.sync_memory_md

    def waiting(*args, **kwargs):
        assert gate.wait(0.2), "the event loop could not release the sync worker"
        return original(*args, **kwargs)

    monkeypatch.setattr("nanobot.agent.tools.memory.sync_memory_md", waiting)
    pending = asyncio.create_task(_tool(SearchMemoryTool, ws).execute(query="oolong"))
    await asyncio.sleep(0.01)
    gate.set()
    output = await pending
    assert "UNVERIFIED" not in output


def test_memory_symlink_outside_workspace_is_not_indexed(tmp_path):
    ws = _ws(tmp_path, None)
    outside = tmp_path / "private.txt"
    outside.write_text("private-outside-marker")
    (ws / "memory/MEMORY.md").symlink_to(outside)
    status = sync_memory_md(ConversationEventStore(ws), str(ws.resolve()))
    assert not status.verified
    assert not _doc_rows(ws)


async def test_whitespace_edit_keeps_old_event_content_immutable(tmp_path):
    ws = _ws(tmp_path)
    old = _ids(await _tool(SearchMemoryTool, ws).execute(query="fake-vim-9000"))[0]
    _edit(ws / "memory/MEMORY.md", DOC.replace("fake-vim-9000", "fake-vim-9000   "))
    output = await _tool(SearchMemoryTool, ws).execute(query="fake-vim-9000")
    assert old not in _ids(output)
    latest = _ids(output)[0]
    raw = await _tool(ReadMemoryEventsTool, ws).execute(event_ids=[latest])
    assert "fake-vim-9000   " in raw


async def test_unverified_document_is_not_complete_current_evidence(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    target = _ids(await _tool(SearchMemoryTool, ws).execute(query="fake-vim-9000"))[0]
    reader = _tool(ReadMemoryEventsTool, ws)
    monkeypatch.setattr(reader, "_sync_document", lambda: DocSyncStatus("unverified", error="test"))
    token = active_evidence_capture.set(True)
    try:
        result = await reader.execute(event_ids=[target])
    finally:
        active_evidence_capture.reset(token)
    assert not result.memory_evidence["memory_complete"]
    assert "fake-vim-9000" not in result
    assert "UNVERIFIED" in result


@pytest.mark.parametrize("mode", ["forget", "purge"])
async def test_shared_forgetting_is_personal_and_explains_remaining_copies(tmp_path, mode):
    ws = _ws(tmp_path)
    target = _ids(await _tool(SearchMemoryTool, ws).execute(query="fake-vim-9000"))[0]
    notice = await _tool(ForgetMemoryEventsTool, ws).execute(event_ids=[target], mode=mode)
    assert "MEMORY.md" in notice and "system prompt" in notice
    assert "not a complete deletion" in notice
    assert "fake-vim-9000" in (ws / "memory/MEMORY.md").read_text()
    # Even an unrelated edit and a fresh store must not reintroduce it for chat-a.
    _edit(ws / "memory/MEMORY.md", DOC + "\n# Other\nUnrelated edit.\n")
    ConversationEventStore(ws).rebuild_derived_indexes()
    hidden = await _tool(SearchMemoryTool, ws, "chat-a").execute(query="fake-vim-9000")
    visible = await _tool(SearchMemoryTool, ws, "chat-b").execute(query="fake-vim-9000")
    assert target not in _ids(hidden) and target in _ids(visible)
    assert "fake-vim-9000" not in await _tool(ReadMemoryEventsTool, ws, "chat-a").execute(event_ids=[target])


@pytest.mark.parametrize("mode", ["forget", "purge"])
async def test_personal_history_forget_and_purge_keep_their_existing_meaning(tmp_path, mode):
    ws = _ws(tmp_path)
    store = ConversationEventStore(ws)
    scope = MemoryScope.from_runtime(owner_id="chat-a", workspace_id=str(ws.resolve()), agent_id=None)
    event = store.append_event(scope=scope, conversation_id="cli:chat-a", session_id="cli:chat-a",
                               actor="user", event_type="USER_MESSAGE", content="private-test-marker")
    result = await _tool(ForgetMemoryEventsTool, ws).execute(event_ids=[event.event_id], mode=mode)
    assert f"{mode} applied to 1 scoped conversation memory event(s)." == result
    assert not store.get_events(scope, [event.event_id])
    with store.connect() as conn:
        row = conn.execute("SELECT content FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
    assert row["content"] == (None if mode == "purge" else "private-test-marker")


async def test_heading_search_covers_following_sections(tmp_path):
    ws = _ws(tmp_path, "# 아침식사 선호\n- first fact\n- second fact\n")
    output = await _tool(SearchMemoryTool, ws).execute(query="아침식사")
    assert len(_ids(output)) == 2
    raw = await _tool(ReadMemoryEventsTool, ws).execute(event_ids=_ids(output))
    assert "first fact" in raw and "second fact" in raw


def test_touch_same_hash_path_rechecks_file_after_read(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    md = ws / "memory/MEMORY.md"
    store = ConversationEventStore(ws)
    sync_memory_md(store, str(ws.resolve()))
    _edit(md, DOC)
    original = type(md).read_bytes

    def racing(path):
        data = original(path)
        if path == md:
            _edit(md, DOC.replace("Heron", "Ibis"))
        return data

    monkeypatch.setattr(type(md), "read_bytes", racing)
    status = sync_memory_md(store, str(ws.resolve()))
    assert not status.verified

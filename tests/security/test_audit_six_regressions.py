"""Regression cases for the six findings in the November 18 code review."""

import asyncio
import json

import pytest

from nanobot.agent.context_selection import IntentDesign, SelectedContext
from nanobot.agent.context_trace import ContextTrace
from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.memory import (
    ForgetMemoryEventsTool,
    ReadMemoryEventsTool,
    SearchMemoryTool,
)
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.memory.task_context import TaskContextStore
from nanobot.webui.http_utils import http_response


def scope(owner="owner", workspace="workspace", agent="agent"):
    return MemoryScope.from_runtime(owner_id=owner, workspace_id=workspace, agent_id=agent)


def append(store, memory_scope, session="chat", kind="USER_MESSAGE", content="PrivateMarker",
           metadata=None):
    return store.append_event(
        scope=memory_scope, conversation_id=session, session_id=session,
        actor="user", event_type=kind, content=content, metadata=metadata,
    )


@pytest.mark.parametrize("tool_class", [
    SearchMemoryTool, ReadMemoryEventsTool, ForgetMemoryEventsTool,
])
@pytest.mark.parametrize("bind_runtime", [False, True])
async def test_shared_memory_tool_keeps_concurrent_owner_scopes(tmp_path, tool_class, bind_runtime):
    tool = tool_class(str(tmp_path))
    a_scope = scope("owner-A", str(tmp_path.resolve()), None)
    b_scope = scope("owner-B", str(tmp_path.resolve()), None)
    a = append(tool.store, a_scope, content="PrivateMarker OwnerAlpha")
    b = append(tool.store, b_scope, content="PrivateMarker OwnerBeta")
    a_ready, b_ready = asyncio.Event(), asyncio.Event()

    async def first():
        ctx = RequestContext(channel="websocket", chat_id="owner-A")
        token = bind_request_context(ctx) if bind_runtime else None
        try:
            tool.set_context(ctx)
            a_ready.set()
            await b_ready.wait()
            if tool_class is SearchMemoryTool:
                result = await tool.execute(query="PrivateMarker")
                assert a.event_id in result and b.event_id not in result
            elif tool_class is ReadMemoryEventsTool:
                result = await tool.execute(event_ids=[a.event_id, b.event_id])
                assert "OwnerAlpha" in result and "OwnerBeta" not in result
            else:
                result = await tool.execute(event_ids=[a.event_id, b.event_id], mode="purge")
                assert "1 scoped conversation memory event" in result
                assert tool.store.get_events(a_scope, [a.event_id]) == []
            assert tool.store.get_events(b_scope, [b.event_id])
        finally:
            if token is not None:
                reset_request_context(token)

    async def second():
        await a_ready.wait()
        tool.set_context(RequestContext(channel="websocket", chat_id="owner-B"))
        b_ready.set()

    await asyncio.gather(first(), second())


async def test_bound_runtime_context_overrides_fallback(tmp_path):
    tool = SearchMemoryTool(str(tmp_path))
    tool.set_context(RequestContext(channel="websocket", chat_id="wrong-owner"))
    token = bind_request_context(RequestContext(channel="websocket", chat_id="right-owner"))
    try:
        assert tool._scope().owner_id == "right-owner"
    finally:
        reset_request_context(token)


@pytest.mark.parametrize("kind,content,metadata", [
    ("USER_MESSAGE", "PrivateMarker", None),
    ("CURATED_MEMORY_EDIT", "- PrivateMarker\n- AnotherMarker", None),
    ("TOOL_RESULT", "PrivateMarker", {"name": "search_memory"}),
    ("TOOL_RESULT", None, {"name": "read_memory_events"}),
])
@pytest.mark.parametrize("forget_first", [False, True])
def test_purge_handles_indexed_unindexed_and_forgotten_events(
    tmp_path, kind, content, metadata, forget_first,
):
    store = ConversationEventStore(tmp_path)
    owner = scope()
    deleted = append(store, owner, kind=kind, content=content, metadata=metadata)
    kept = append(store, owner, session="kept", content="KeptSearchMarker")
    if forget_first:
        assert store.forget_events(owner, [deleted.event_id]) == 1
        assert store.forget_events(owner, [deleted.event_id]) == 0
    assert store.purge_session("chat") == 1
    assert store.purge_session("chat") == 0
    assert store.get_events(owner, [kept.event_id])
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE session_id='chat'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM event_chunks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'KeptSearchMarker'"
        ).fetchone()[0] == 1
        conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
        conn.execute("INSERT INTO event_chunks_fts(event_chunks_fts) VALUES('integrity-check')")


def test_forget_then_payload_purge_is_idempotent_and_removes_metadata(tmp_path):
    store = ConversationEventStore(tmp_path)
    owner = scope()
    event = append(store, owner, metadata={"private": "fixture"})
    assert store.forget_events(owner, [event.event_id]) == 1
    assert store.forget_events(owner, [event.event_id], purge=True) == 1
    assert store.forget_events(owner, [event.event_id], purge=True) == 0
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE event_id=?", (event.event_id,)).fetchone()
        assert row["content"] is row["metadata_json"] is row["content_hash"] is None
        assert row["redacted_at"]


def test_purge_handles_legacy_index_membership(tmp_path):
    store = ConversationEventStore(tmp_path)
    event = append(store, scope(), kind="CURATED_MEMORY_EDIT", content="- LegacyMarker")
    with store.connect() as conn:
        rowid = conn.execute(
            "SELECT event_rowid FROM events WHERE event_id=?", (event.event_id,)
        ).fetchone()[0]
        conn.execute("INSERT INTO events_fts(rowid,content) VALUES(?,?)", (rowid, event.content))
        # An older/partial chunk index need not contain every source chunk.
        conn.execute("DELETE FROM event_chunks_fts")
    assert store.purge_session("chat") == 1
    with store.connect() as conn:
        conn.execute("INSERT INTO events_fts(events_fts) VALUES('integrity-check')")
        conn.execute("INSERT INTO event_chunks_fts(event_chunks_fts) VALUES('integrity-check')")


def test_purge_removes_task_versions_before_parent_tasks(tmp_path):
    store = ConversationEventStore(tmp_path)
    tasks = TaskContextStore(store)
    owner = scope()
    deleted = append(store, owner)
    kept = append(store, owner, session="kept")
    tasks.create(owner, "chat", deleted, "private task", ["done"])
    kept_task = tasks.create(owner, "kept", kept, "kept task", ["done"])
    assert store.purge_session("chat") == 1
    assert tasks.list(owner, "chat") == []
    assert tasks.list(owner, "kept")[0]["task_id"] == kept_task["task_id"]


@pytest.mark.parametrize("purge", [False, True])
def test_forget_enforces_owner_workspace_and_agent(tmp_path, purge):
    store = ConversationEventStore(tmp_path)
    allowed = scope()
    visible = append(store, allowed)
    other_scopes = [
        scope(owner="another-owner"),
        scope(workspace="another-workspace"),
        scope(agent="another-agent"),
    ]
    hidden = [append(store, other) for other in other_scopes]
    assert store.forget_events(
        allowed, [visible.event_id, *(event.event_id for event in hidden)], purge=purge
    ) == 1
    for other, event in zip(other_scopes, hidden):
        assert store.get_events(other, [event.event_id])[0].content == "PrivateMarker"


@pytest.mark.parametrize("status", ["valid", "superseded", "insufficient_evidence", "conflict"])
def test_execution_context_preserves_verified_carried_conditions(status):
    design = IntentDesign.model_validate(dict(
        goal="Explain arithmetic", request_type="explain", task_relation="new",
        task_id=None, route="direct", constraints=[], information_needs=[],
        capabilities_needed=[], completion_checks=["Answer supplied"], unresolved=[], answer="4",
    ))
    design._carried = [dict(
        kind="permission", text="OLD_REQUIREMENT", source_id="E-old", task_id="task",
        status=status, reason="verified against current instruction", necessity="forbidden",
    )]
    selected = SelectedContext(
        trace=ContextTrace("audit-six"),
        design=design,
        state={"carried_conditions": [{"text": "OLD_REQUIREMENT", "status": "valid"}]},
    )
    result = json.loads(selected.execution_context())
    assert result["carried_conditions"] == selected.carried_report()
    assert result["carried_conditions"][0]["status"] == status
    assert "effect" in result["carried_conditions"][0]


def test_http_extra_headers_replace_defaults_case_insensitively():
    response = http_response(b"fixture", extra_headers=[
        ("x-content-type-options", "nosniff"),
        ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ])
    assert response.headers.get_all("X-Content-Type-Options") == ["nosniff"]
    assert response.headers.get_all("Content-Security-Policy") == [
        "default-src 'none'; frame-ancestors 'none'"
    ]

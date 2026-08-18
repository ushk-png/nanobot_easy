from pathlib import Path

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.memory.raw_reader import read_memory_events
from nanobot.memory.search import MemorySearcher, format_search_result


def test_event_store_search_read_and_forget(tmp_path: Path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")
    other = MemoryScope.from_runtime(owner_id="u2", workspace_id="main", agent_id="nanobot")

    e1 = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="user",
        event_type="USER_MESSAGE",
        content="registry.py에서 ToolRegistry intent_summary 적용해줘",
    )
    e2 = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="assistant",
        event_type="ASSISTANT_MESSAGE",
        content="registry.py에 intent metadata 검증을 추가했습니다.",
    )
    store.append_event(
        scope=other,
        conversation_id="c2",
        session_id="s2",
        actor="assistant",
        event_type="ASSISTANT_MESSAGE",
        content="registry.py 다른 사용자 기록",
    )

    result = MemorySearcher(store).search(scope=scope, query="ToolRegistry intent_summary registry.py", limit=5)
    ids = {event.event_id for window in result.windows for event in window.events}
    assert e1.event_id in ids
    assert e2.event_id in ids
    assert all("u2" not in event.event_id for window in result.windows for event in window.events)

    raw = read_memory_events(store, scope, [e2.event_id])
    assert raw[0].content == "registry.py에 intent metadata 검증을 추가했습니다."

    assert store.forget_events(scope, [e2.event_id]) == 1
    result_after = MemorySearcher(store).search(scope=scope, query="intent metadata 검증", limit=5)
    ids_after = {event.event_id for window in result_after.windows for event in window.events}
    assert e2.event_id not in ids_after


def test_curated_memory_is_chunk_indexed_without_whole_event_fts(tmp_path: Path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")
    curated = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="dream",
        event_type="CURATED_MEMORY_EDIT",
        content="- Alpha project decided Postgres\n- Beta roadmap chooses SQLite\n- Gamma note unrelated",
    )

    with store.connect() as conn:
        chunks = conn.execute("SELECT chunk_id, ordinal, text FROM event_chunks WHERE event_id = ? ORDER BY ordinal", (curated.event_id,)).fetchall()
        whole_rows = conn.execute(
            """
            SELECT e.event_id
            FROM events_fts
            JOIN events e ON e.event_rowid = events_fts.rowid
            WHERE events_fts MATCH ? AND e.event_id = ?
            """,
            ('"Beta roadmap chooses SQLite"', curated.event_id),
        ).fetchall()
    assert [row["ordinal"] for row in chunks] == [1, 2, 3]
    assert whole_rows == []

    result = MemorySearcher(store).search(scope=scope, query="Beta roadmap SQLite", limit=5)
    ids = {event.event_id for window in result.windows for event in window.events}
    assert curated.event_id in ids


def test_search_result_display_is_compact_for_curated_and_tool_events(tmp_path: Path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")

    curated = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="dream",
        event_type="CURATED_MEMORY_EDIT",
        content="Conversation RAG Memory " + ("상세 구현 기록 " * 1000),
    )
    store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="assistant",
        event_type="ASSISTANT_MESSAGE",
        content="unmatched nearby chatter " * 200,
    )
    result = MemorySearcher(store).search(scope=scope, query="Conversation RAG Memory", limit=5, max_context_tokens=4000)
    rendered = format_search_result(result)

    assert curated.event_id in rendered
    assert "mode: compact_snippets" in rendered
    assert "event_id:" in rendered
    assert "snippet:" in rendered
    assert "[snippet_truncated=true; use read_memory_events for raw content]" in rendered
    assert "unmatched nearby chatter" not in rendered
    assert "retrieved_context_tokens_estimate:" in rendered
    assert len(rendered) < 4000


def test_memory_tool_results_are_not_indexed_and_can_be_redacted(tmp_path: Path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")
    event = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="tool",
        event_type="TOOL_RESULT",
        content="Memory Search Result\nquery: secret contamination\n[abc] huge derived search output",
        metadata={"name": "search_memory", "tool_call_id": "call-1"},
    )

    result = MemorySearcher(store).search(scope=scope, query="secret contamination", limit=5)
    ids = {item.event_id for window in result.windows for item in window.events}
    assert event.event_id not in ids

    assert store.redact_memory_tool_results() == 1
    raw = read_memory_events(store, scope, [event.event_id])
    assert raw == []


def test_rebuild_derived_indexes_drops_existing_memory_tool_result_fts(tmp_path: Path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")
    event = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="tool",
        event_type="TOOL_RESULT",
        content="Memory Search Result\nquery: old pollution token",
        metadata={"name": "search_memory"},
    )
    with store.connect() as conn:
        rowid = conn.execute("SELECT event_rowid FROM events WHERE event_id = ?", (event.event_id,)).fetchone()["event_rowid"]
        conn.execute("INSERT INTO events_fts(rowid, content) VALUES (?, ?)", (rowid, event.content))

    with store.connect() as conn:
        fts_rows = conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH ?", ('"old pollution token"',)).fetchall()
    assert [row["rowid"] for row in fts_rows] == [rowid]

    store.rebuild_derived_indexes()
    clean = MemorySearcher(store).search(scope=scope, query="old pollution token", limit=5)
    assert event.event_id not in {item.event_id for window in clean.windows for item in window.events}


def test_short_korean_terms_use_like_fallback(tmp_path: Path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")
    event = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="user",
        event_type="USER_MESSAGE",
        content="메모리 도구 TOOL_RESULT 오염 차단 설계",
    )

    result = MemorySearcher(store).search(scope=scope, query="오염", limit=5)
    ids = {item.event_id for window in result.windows for item in window.events}

    assert event.event_id in ids


def test_memory_cleanup_cli_redacts_and_rebuilds_indexes(tmp_path: Path, capsys):
    from nanobot.memory.importer import main

    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id=str(tmp_path.resolve()), agent_id=None)
    event = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="tool",
        event_type="TOOL_RESULT",
        content="Memory Search Result\nquery: cli pollution token",
        metadata={"name": "search_memory"},
    )

    # Simulate an older DB where the payload had already leaked into FTS.
    with store.connect() as conn:
        rowid = conn.execute("SELECT event_rowid FROM events WHERE event_id = ?", (event.event_id,)).fetchone()["event_rowid"]
        conn.execute("INSERT INTO events_fts(rowid, content) VALUES (?, ?)", (rowid, event.content))

    main(["redact-memory-tool-results", "--workspace", str(tmp_path)])
    output = capsys.readouterr().out

    assert '"redacted_memory_tool_result_events": 1' in output
    assert read_memory_events(store, scope, [event.event_id]) == []
    with store.connect() as conn:
        fts_rows = conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH ?", ('"cli pollution token"',)).fetchall()
    assert fts_rows == []


def test_search_result_has_global_formatted_char_cap(tmp_path: Path):
    from nanobot.memory.search import FORMATTED_RESULT_CHAR_BUDGET

    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")
    for idx in range(80):
        store.append_event(
            scope=scope,
            conversation_id="c1",
            session_id=f"s{idx}",
            actor="assistant",
            event_type="ASSISTANT_MESSAGE",
            content=f"global cap token {idx} " + ("verbose payload " * 200),
        )

    result = MemorySearcher(store).search(scope=scope, query="global cap token", limit=50, max_context_tokens=20_000)
    rendered = format_search_result(result)

    assert len(rendered) <= FORMATTED_RESULT_CHAR_BUDGET + 100
    if "formatted_search_result_truncated=true" in rendered:
        assert "use read_memory_events for raw content" in rendered

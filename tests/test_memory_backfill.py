"""The backfill adds only the turns the memory database missed, safely and repeatably."""

from __future__ import annotations

from pathlib import Path

from nanobot.memory.backfill import backfill_missing_events
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.session.manager import SessionManager


def _seed(workspace: Path) -> tuple[ConversationEventStore, str]:
    sessions = SessionManager(workspace)
    session = sessions.get_or_create("telegram:4242")
    session.add_message("user", "old question")
    session.add_message("assistant", "old answer")
    session.add_message("user", "question after /new")
    session.add_message("assistant", "answer after /new")
    session.add_message("assistant", None, tool_calls=[{"id": "c", "type": "function",
                                                         "function": {"name": "x", "arguments": "{}"}}])
    sessions.save(session)

    store = ConversationEventStore(workspace)
    scope = MemoryScope.from_runtime(owner_id="4242", workspace_id=str(workspace.resolve()), agent_id=None)
    # The first two messages were recorded live (same timestamps); the later two were lost.
    for message in session.messages[:2]:
        store.append_event(
            scope=scope, conversation_id="telegram:4242", session_id="telegram:4242",
            actor=message["role"], event_type="USER_MESSAGE" if message["role"] == "user" else "ASSISTANT_MESSAGE",
            content=message["content"], ts=message["timestamp"],
        )
    return store, "telegram:4242"


def _contents(store: ConversationEventStore) -> list[str]:
    with store.connect() as conn:
        return [row[0] for row in conn.execute("SELECT content FROM events ORDER BY event_rowid")]


def test_a_dry_run_reports_the_gap_and_writes_nothing(tmp_path) -> None:
    store, key = _seed(tmp_path)
    report = backfill_missing_events(tmp_path)
    assert (report.sessions, report.missing, report.inserted) == (1, 2, 0)
    assert report.per_session == {key: 2}
    assert _contents(store) == ["old question", "old answer"]
    assert not list(store.db_path.parent.glob("*.bak-*"))


def test_apply_inserts_only_the_missing_turns_after_a_backup(tmp_path) -> None:
    store, _ = _seed(tmp_path)
    report = backfill_missing_events(tmp_path, apply=True)
    assert report.inserted == 2
    assert report.backup is not None and report.backup.exists()
    assert _contents(store) == ["old question", "old answer", "question after /new", "answer after /new"]


def test_running_it_again_adds_nothing(tmp_path) -> None:
    store, _ = _seed(tmp_path)
    backfill_missing_events(tmp_path, apply=True)
    again = backfill_missing_events(tmp_path, apply=True)
    assert again.missing == 0 and again.inserted == 0
    assert len(_contents(store)) == 4


def test_backfilled_turns_are_found_by_memory_search(tmp_path) -> None:
    from nanobot.memory.search import MemorySearcher

    store, _ = _seed(tmp_path)
    backfill_missing_events(tmp_path, apply=True)
    scope = MemoryScope.from_runtime(owner_id="4242", workspace_id=str(tmp_path.resolve()), agent_id=None)
    found = MemorySearcher(store).search(scope=scope, query="question after", limit=5)
    texts = " ".join(event.content or "" for window in found.windows for event in window.events)
    assert "question after /new" in texts


def test_a_query_with_no_match_does_not_return_unrelated_recent_events(tmp_path) -> None:
    from nanobot.memory.search import MemorySearcher, format_search_result

    store, _ = _seed(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="4242", workspace_id=str(tmp_path.resolve()), agent_id=None)
    searcher = MemorySearcher(store)

    missed = searcher.search(scope=scope, query="zzzxqv-never-said", limit=5)
    assert not missed.windows
    assert "no_matches" in format_search_result(missed)

    # A blank query is a browse request: recent events are still returned.
    browsed = searcher.search(scope=scope, query="", limit=5)
    assert browsed.windows

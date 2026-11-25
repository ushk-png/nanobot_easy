"""New turns after /new or auto-compaction must still reach conversation_memory.db (review C3-01).

With contextSelection off, the DB event id is the message's list index. The index restarts after
the session is cleared or compacted, and the store ignores a repeated id, so later turns were
silently not recorded.
"""

from __future__ import annotations

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.memory.event_store import ConversationEventStore
from nanobot.session.manager import Session


def _loop(tmp_path) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)  # only the attributes the method uses
    loop.conversation_memory = ConversationEventStore(tmp_path)
    loop._memory_workspace_id = str(tmp_path.resolve())
    loop.context_selection_config = None
    return loop


def _store_turn(loop, session, user: str, assistant: str) -> None:
    msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="42", content="x", metadata={})
    start = len(session.messages)
    session.add_message("user", user)
    session.add_message("assistant", assistant)
    loop.conversation_memory.insert_events(
        loop._memory_events_from_session_messages(
            msg=msg, session=session, messages=session.messages, start_index=start
        )
    )


def _rows(loop) -> list[tuple[str, str, int]]:
    with loop.conversation_memory.connect() as conn:
        return [
            (row["event_id"], row["content"], row["sequence"])
            for row in conn.execute("SELECT event_id, content, sequence FROM events ORDER BY event_rowid")
        ]


def test_turns_after_clear_are_recorded_and_old_rows_stay(tmp_path) -> None:
    loop = _loop(tmp_path)
    session = Session(key="telegram:42")
    _store_turn(loop, session, "old question A", "old answer A")
    _store_turn(loop, session, "old question B", "old answer B")
    before = _rows(loop)
    session.clear()  # what /new does
    _store_turn(loop, session, "NEW question after /new", "NEW answer after /new")

    rows = _rows(loop)
    assert rows[: len(before)] == before  # nothing rewritten
    assert [content for _, content, _ in rows[len(before):]] == [
        "NEW question after /new",
        "NEW answer after /new",
    ]
    assert len({event_id for event_id, _, _ in rows}) == len(rows)
    new_sequences = [seq for _, _, seq in rows[len(before):]]
    assert min(new_sequences) > max(seq for _, _, seq in before)


def test_saving_the_same_message_twice_does_not_duplicate_it(tmp_path) -> None:
    """The early user-message save and the end-of-turn save both cover the same message."""
    loop = _loop(tmp_path)
    session = Session(key="telegram:42")
    msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="42", content="x", metadata={})
    session.add_message("user", "hello")
    for _ in range(2):
        loop.conversation_memory.insert_events(
            loop._memory_events_from_session_messages(
                msg=msg, session=session, messages=session.messages, start_index=0
            )
        )
    assert [content for _, content, _ in _rows(loop)] == ["hello"]


def test_compaction_that_restarts_the_index_does_not_swallow_new_turns(tmp_path) -> None:
    loop = _loop(tmp_path)
    session = Session(key="telegram:42")
    for index in range(4):
        _store_turn(loop, session, f"question {index}", f"answer {index}")
    session.messages = session.messages[-2:]  # idle compaction keeps only the recent tail
    _store_turn(loop, session, "after compaction", "reply after compaction")
    contents = [content for _, content, _ in _rows(loop)]
    assert "after compaction" in contents and "reply after compaction" in contents

"""A per-request control message is stored as a hidden ``user`` message right after the real request. It is not a
turn start: every place that cuts the history at "the nearest user message" must land on the real request, so the
request text is never archived while the rest of its turn stays in the live session."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.memory import Consolidator, MemoryStore, _recent_start_index_by_tokens
from nanobot.providers.base import LLMResponse
from nanobot.session.history_visibility import is_turn_start_message
from nanobot.session.manager import Session, SessionManager

CONTROL = "[Task evidence control; current request_id={rid}] Use update_task in the existing tool loop."


def real(text: str) -> dict:
    return {"role": "user", "content": text}


def control(rid: str) -> dict:
    return {"role": "user", "_hidden_history": True, "_task_control": True, "content": CONTROL.format(rid=rid)}


def recovery(rid: str) -> dict:
    return {"role": "user", "_hidden_history": True, "content": f"Completion defects (all): {rid}"}


def tool_step(n: int, body: str = "result") -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{n}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": body},
    ]


def request_turn(tag: str, steps: int, *, body: str = "result") -> list[dict]:
    """real request, hidden control, a tool loop, final answer"""
    out = [real(f"REQUEST-{tag}"), control(tag)]
    for n in range(steps):
        out += tool_step(n, body)
    out.append({"role": "assistant", "content": f"ANSWER-{tag}"})
    return out


def contents(messages: list[dict]) -> list[str]:
    return [str(m.get("content", "")) for m in messages]


# ---- the predicate ------------------------------------------------------------------------------------------

def test_only_a_visible_user_message_starts_a_turn():
    assert is_turn_start_message(real("hello"))
    assert not is_turn_start_message(control("a"))
    assert not is_turn_start_message(recovery("a"))
    assert not is_turn_start_message({"role": "assistant", "content": "x"})
    assert not is_turn_start_message({"role": "tool", "content": "x"})


def test_hidden_user_text_that_is_not_task_control_still_starts_a_turn():
    # automation / injected user text is replayed by get_history, so it is a legitimate turn start
    assert is_turn_start_message({"role": "user", "_hidden_history": True, "content": "scheduled prompt"})


# ---- Session.retain_recent_legal_suffix (idle compaction, /new, session caps) ----------------------------------

def test_retained_suffix_starts_at_the_real_request_not_its_control_message():
    session = Session(key="t")
    session.messages = request_turn("OLD", 1) + request_turn("LAST", 7)       # last turn is longer than the window
    result = session.retain_recent_legal_suffix(8, extend_to_user=True)
    assert contents(session.messages)[0] == "REQUEST-LAST"
    assert "REQUEST-LAST" not in contents(result.dropped)


def test_hard_cap_anchors_on_the_real_request_when_the_tail_has_no_user_message():
    session = Session(key="t")
    session.messages = request_turn("LAST", 6)
    session.retain_recent_legal_suffix(4)
    assert contents(session.messages)[0] == "REQUEST-LAST"


def test_a_tail_that_only_holds_the_control_message_is_extended_back_to_the_request():
    session = Session(key="t")
    # window of 3 covers only the last tool step, then extend_to_user walks back
    session.messages = [real("REQUEST-X"), control("X"), *tool_step(0), *tool_step(1), *tool_step(2)]
    session.retain_recent_legal_suffix(3, extend_to_user=True)
    assert contents(session.messages)[0] == "REQUEST-X"


# ---- Consolidator -------------------------------------------------------------------------------------------

def make(tmp_path):
    store = MemoryStore(tmp_path)
    sessions = SessionManager(tmp_path)
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="SUMMARY", finish_reason="stop"))
    c = Consolidator(store=store, provider=provider, model="m", sessions=sessions, context_window_tokens=100_000,
                     build_messages=MagicMock(return_value=[]), get_tool_definitions=MagicMock(return_value=[]),
                     max_completion_tokens=100)
    c._SAFETY_BUFFER = 0
    return c, sessions, provider


async def test_idle_compaction_keeps_the_request_of_an_unfinished_long_turn(tmp_path):
    c, sessions, provider = make(tmp_path)
    session = sessions.get_or_create("cli:t")
    session.messages = request_turn("OLD", 1) + request_turn("LAST", 9)[:-1]   # last turn did not finish
    sessions.save(session)

    assert await c.compact_idle_session("cli:t", max_suffix=8) == "SUMMARY"

    kept = sessions.get_or_create("cli:t").messages
    assert contents(kept)[0] == "REQUEST-LAST"
    assert "REQUEST-OLD" not in contents(kept)                                   # only the finished turn was archived
    assert len(kept) == len(request_turn("LAST", 9)) - 1                         # the whole unfinished turn is kept


def test_a_token_boundary_is_never_a_control_message(tmp_path):
    c, sessions, _ = make(tmp_path)
    session = sessions.get_or_create("cli:t")
    session.messages = request_turn("A", 3, body="x " * 400) + request_turn("B", 1)
    idx, _removed = c.pick_consolidation_boundary(session, 1)
    assert contents(session.messages)[idx] == "REQUEST-B"


def test_a_replay_start_recovers_the_real_request_not_the_control_message():
    messages = [real("REQUEST-A"), control("A"), *tool_step(0), *tool_step(1), *tool_step(2)]
    start = _recent_start_index_by_tokens(messages, 1, extend_to_user=True)       # budget keeps only the last step
    assert contents(messages)[start] == "REQUEST-A"


def test_a_summary_cut_never_separates_a_request_from_its_control_message(tmp_path):
    c, _sessions, _ = make(tmp_path)
    messages = [real("R1"), control("1"), *tool_step(0), real("R2"), control("2"), {"role": "assistant", "content": "a"}]
    c._summary_fits = lambda msgs, key: len(msgs) <= 5                  # R1 turn (4 messages) + R2 would not fit
    cut = c._fit_end(messages, 0, len(messages), "cli:t")
    assert cut == 4 and contents(messages)[cut] == "R2"

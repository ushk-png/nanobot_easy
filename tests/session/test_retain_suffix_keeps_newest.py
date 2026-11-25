"""Trimming a long turn must keep the newest messages, not the oldest (review C2-12)."""

from __future__ import annotations

from nanobot.session.manager import Session


def _long_turn(rounds: int) -> Session:
    session = Session(key="heartbeat")
    session.add_message("user", "check everything")
    for index in range(rounds):
        session.add_message(
            "assistant", None,
            tool_calls=[{"id": f"call{index}", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}],
        )
        session.add_message("tool", f"result {index}", tool_call_id=f"call{index}", name="read_file")
    session.add_message("assistant", "final answer")
    return session


def _is_legal(messages: list[dict]) -> bool:
    open_calls: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            open_calls |= {call["id"] for call in message.get("tool_calls") or []}
        elif message.get("role") == "tool" and message.get("tool_call_id") not in open_calls:
            return False
    return True


def test_trimming_a_long_turn_keeps_the_final_answer_and_a_legal_sequence() -> None:
    session = _long_turn(rounds=6)  # 1 + 12 + 1 = 14 messages
    session.retain_recent_legal_suffix(8)
    assert session.messages[-1]["content"] == "final answer"
    assert session.messages[0]["role"] == "user" and session.messages[0]["content"] == "check everything"
    assert len(session.messages) <= 8
    assert _is_legal(session.messages)
    contents = [str(m.get("content")) for m in session.messages]
    assert "result 5" in contents  # the latest tool result survives
    assert "result 0" not in contents  # the earliest ones were dropped


def test_a_one_message_window_keeps_just_the_request() -> None:
    session = _long_turn(rounds=3)
    session.retain_recent_legal_suffix(1)
    assert [m["role"] for m in session.messages] == ["user"]

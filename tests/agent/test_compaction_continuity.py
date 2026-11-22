"""Compaction must carry task context forward and retain originals on failure."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.memory import Consolidator, MemoryStore
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import SessionManager


def make_consolidator(tmp_path, response=None):
    sessions = SessionManager(tmp_path)
    provider = SimpleNamespace(chat_with_retry=AsyncMock(
        return_value=response or LLMResponse(content="- [ephemeral] Next: check report.txt."),
    ))
    consolidator = Consolidator(
        MemoryStore(tmp_path), provider, "test-model", sessions, 128_000,
        lambda **kwargs: [], lambda: [],
    )
    session = sessions.get_or_create("cli:continuity")
    session.metadata["_last_summary"] = {
        "text": "- [ephemeral] Goal: fix report.txt; never change production.json.",
        "last_active": session.updated_at.isoformat(),
    }
    for index in range(8):
        session.add_message("user", f"Continue step {index} for report.txt.")
        session.add_message("assistant", f"Step {index} is still pending.")
    sessions.save(session)
    return consolidator, session, provider


async def test_archive_receives_previous_summary_and_current_revocation(tmp_path):
    consolidator, session, provider = make_consolidator(tmp_path)
    await consolidator.archive([
        {"role": "user", "content": "Cancel the earlier write permission; review only."},
    ], session_key=session.key)
    messages = provider.chat_with_retry.await_args.kwargs["messages"]
    assert "production.json" in messages[1]["content"]
    assert "Cancel the earlier write permission" in messages[1]["content"]
    assert "not authorization" in messages[0]["content"]


@pytest.mark.parametrize("finish_reason,content", [
    ("error", "provider unavailable"), ("stop", ""), ("length", "partial summary"),
    pytest.param("stop", "x" * 8001, id="oversize"),
])
async def test_idle_compaction_failure_preserves_live_messages(tmp_path, finish_reason, content):
    consolidator, session, _ = make_consolidator(
        tmp_path, LLMResponse(content=content, finish_reason=finish_reason),
    )
    before = deepcopy(session.messages)
    prior = deepcopy(session.metadata)
    result = await consolidator.compact_idle_session(session.key)
    consolidator.sessions.invalidate(session.key)
    restored = consolidator.sessions.get_or_create(session.key)
    assert result is None
    assert restored.messages == before
    assert restored.last_consolidated == 0
    assert restored.metadata == prior


async def test_replay_compaction_failure_keeps_cursor(tmp_path):
    consolidator, session, provider = make_consolidator(tmp_path)
    provider.chat_with_retry.side_effect = TimeoutError("offline")
    before = deepcopy(session.messages)
    # ~95 tokens keeps the last 8 of these 16 short messages (~11-12 tokens each),
    # the same "archive half, keep half" shape the old message-count=8 cap gave.
    result = await consolidator._consolidate_replay_overflow(session, 95)
    assert result is None
    assert session.last_consolidated == 0
    assert session.messages == before


async def test_success_keeps_recent_tool_pair_and_previous_summary_input(tmp_path):
    consolidator, session, provider = make_consolidator(tmp_path)
    session.add_message("user", "Read report.txt and report remaining work.")
    session.add_message("assistant", "", tool_calls=[{
        "id": "read-1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"report.txt"}'},
    }])
    session.add_message("tool", "1| pending", name="read_file", tool_call_id="read-1")
    session.add_message("assistant", "One item remains.")
    consolidator.sessions.save(session)
    result = await consolidator.compact_idle_session(session.key)
    restored = consolidator.sessions.get_or_create(session.key)
    assert result
    assert any(m.get("tool_call_id") == "read-1" for m in restored.messages)
    assert any(m.get("tool_calls") for m in restored.messages)
    assert "production.json" in provider.chat_with_retry.await_args.kwargs["messages"][1]["content"]


async def test_failed_replay_summary_is_not_retried_in_same_preflight(tmp_path):
    consolidator, session, provider = make_consolidator(tmp_path)
    provider.chat_with_retry.side_effect = TimeoutError("offline")
    consolidator.estimate_session_prompt_tokens = lambda _: (200_000, "test")
    await consolidator.maybe_consolidate_by_tokens(session, replay_max_tokens=95)
    assert provider.chat_with_retry.await_count == 1
    assert session.last_consolidated == 0


async def test_multiple_token_rounds_feed_the_latest_checkpoint_to_the_next(tmp_path, monkeypatch):
    consolidator, session, provider = make_consolidator(tmp_path)
    provider.chat_with_retry.side_effect = [
        LLMResponse(content="- [ephemeral] ROUND_ONE: protected.json stays unchanged."),
        LLMResponse(content="- [ephemeral] ROUND_TWO: next, inspect report.txt."),
    ]
    estimates = iter([200_000, 100_000, 1000])
    consolidator.estimate_session_prompt_tokens = lambda _: (next(estimates), "test")
    monkeypatch.setattr("nanobot.agent.memory.estimate_message_tokens", lambda _: 50_000)
    await consolidator.maybe_consolidate_by_tokens(session)
    assert provider.chat_with_retry.await_count == 2
    second_input = provider.chat_with_retry.await_args_list[1].kwargs["messages"][1]["content"]
    assert "ROUND_ONE" in second_input
    assert "ROUND_TWO" in session.metadata["_last_summary"]["text"]


async def test_failed_token_summary_keeps_source_cursor_and_previous_checkpoint(tmp_path):
    consolidator, session, provider = make_consolidator(tmp_path)
    provider.chat_with_retry.side_effect = TimeoutError("offline")
    consolidator.estimate_session_prompt_tokens = lambda _: (200_000, "test")
    before = deepcopy(session.messages)
    prior = deepcopy(session.metadata)
    await consolidator.maybe_consolidate_by_tokens(session)
    assert provider.chat_with_retry.await_count == 1
    assert session.last_consolidated == 0
    assert session.messages == before
    assert session.metadata == prior

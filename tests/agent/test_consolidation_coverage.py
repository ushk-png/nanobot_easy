"""A summary may only cover what the summarizer was given: the range is cut at a user-turn boundary that fits the
input budget, and the cursor never moves past text that was not summarized (token loop and idle compaction)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.memory import _ARCHIVE_SUMMARY_MAX_CHARS, Consolidator, MemoryStore
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import estimate_prompt_tokens

WORDS = 400                                   # a turn of this many words is a few hundred tokens


def body(tag: str, words: int = WORDS) -> str:
    return f"{tag} " + " ".join(f"w{i}" for i in range(words))


def turn(session, n: int, words: int = WORDS, *, tools: bool = False):
    session.add_message("user", body(f"USER{n}", words))
    if tools:
        session.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{n}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        session.messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": f"RESULT{n}"})
    session.add_message("assistant", f"ANSWER{n}")


def make(tmp_path, *, turns: int, words: int = WORDS, tools: bool = False, reply="SUMMARY", finish="stop"):
    store = MemoryStore(tmp_path)
    sessions = SessionManager(tmp_path)
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content=reply, finish_reason=finish))
    c = Consolidator(store=store, provider=provider, model="m", sessions=sessions, context_window_tokens=100_000,
                     build_messages=MagicMock(return_value=[]), get_tool_definitions=MagicMock(return_value=[]),
                     max_completion_tokens=100)
    c._SAFETY_BUFFER = 0
    session = sessions.get_or_create("cli:t")
    for n in range(turns):
        turn(session, n, words, tools=tools)
    sessions.save(session)
    return c, session, provider, store


def request_tokens(c, session, upto_turn: int, key="cli:t") -> int:
    users = [i for i, m in enumerate(session.messages) if m["role"] == "user"] + [len(session.messages)]
    msgs = session.messages[: users[upto_turn]]
    return estimate_prompt_tokens(c._summary_request_messages(c._with_previous_summary(c._format_for_summary(msgs), key)))


def set_budget(c, tokens: int) -> None:
    c.context_window_tokens = tokens + c.max_completion_tokens        # budget = window - completion (no buffer)
    assert c._input_token_budget == tokens


def pressure(c):
    c.estimate_session_prompt_tokens = MagicMock(side_effect=[(10**7, "t"), (0, "t")])   # one round only


def sent(provider, call=0) -> str:
    return provider.chat_with_retry.await_args_list[call].kwargs["messages"][1]["content"]


# ---- 1, 2, 3, 8: range selection ----------------------------------------------------------------------------

async def test_only_the_turns_that_fit_are_summarized_and_marked_done(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=6)
    set_budget(c, request_tokens(c, session, 3) + 20)                  # three turns fit, four do not
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    text = sent(provider)
    assert all(f"USER{n} " in text for n in range(3)) and "USER3 " not in text
    assert session.messages[session.last_consolidated]["content"].startswith("USER3 ")
    assert session.last_consolidated == 6                              # 3 turns x (user, assistant)
    assert "[corrupted]" not in text and "w399" in text                # the text that fit was not cut


async def test_the_latest_condition_after_the_covered_range_stays_unprocessed(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=3)
    session.add_message("user", "승인 취소: 지금까지 요청한 발송은 모두 취소합니다")
    session.add_message("assistant", "취소했습니다")
    set_budget(c, request_tokens(c, session, 2) + 20)
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    assert "승인 취소" not in sent(provider)
    tail = [m["content"] for m in session.messages[session.last_consolidated:]]
    assert any("승인 취소" in t for t in tail)                          # still live, still to be processed


async def test_the_cut_never_separates_a_tool_call_from_its_result(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=6, tools=True)
    for tokens_for_turns in (1, 2, 3, 4):
        c.sessions.invalidate("cli:t")
        fresh = c.sessions.get_or_create("cli:t")
        fresh.last_consolidated = 0
        set_budget(c, request_tokens(c, fresh, tokens_for_turns) + 10)
        pressure(c)
        await c.maybe_consolidate_by_tokens(fresh)
        assert fresh.messages[fresh.last_consolidated]["role"] == "user"
        covered = fresh.messages[: fresh.last_consolidated]
        assert sum(1 for m in covered if m.get("tool_calls")) == sum(1 for m in covered if m["role"] == "tool")


async def test_a_small_range_is_summarized_as_before(tmp_path):
    c, session, provider, store = make(tmp_path, turns=2, words=20)
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    assert provider.chat_with_retry.await_count == 1
    assert session.messages[session.last_consolidated]["content"].startswith("USER1 ")     # the latest turn stays live
    assert session.metadata["_last_summary"]["text"] == "SUMMARY"
    assert any(e["content"] == "SUMMARY" for e in store._read_entries())


# ---- 4, 5: nothing fits -> no call, no change -----------------------------------------------------------------

async def test_a_first_turn_over_budget_makes_no_call_and_keeps_everything(tmp_path):
    c, session, provider, store = make(tmp_path, turns=3)
    session.metadata["_last_summary"] = {"text": "OLD CHECKPOINT", "last_active": session.updated_at.isoformat()}
    before = ([dict(m) for m in session.messages], session.last_consolidated, dict(session.metadata))
    set_budget(c, request_tokens(c, session, 1) - 150)                 # not even one turn fits
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    assert provider.chat_with_retry.await_count == 0
    assert ([dict(m) for m in session.messages], session.last_consolidated, dict(session.metadata)) == before
    assert store._read_entries() == []                                  # no raw dump either
    assert "first turn" in c._summary_stop["cli:t"]


async def test_the_earlier_summary_is_counted_and_never_cut(tmp_path):
    c, session, provider, store = make(tmp_path, turns=2, words=60)
    old = "OLD " + "s" * (_ARCHIVE_SUMMARY_MAX_CHARS - 10)
    session.metadata["_last_summary"] = {"text": old, "last_active": session.updated_at.isoformat()}
    one_turn_without_summary = estimate_prompt_tokens(c._summary_request_messages(c._format_for_summary(session.messages[:2])))
    set_budget(c, one_turn_without_summary + 50)                        # the turn fits alone, not with the summary
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    assert provider.chat_with_retry.await_count == 0
    assert session.metadata["_last_summary"]["text"] == old and session.last_consolidated == 0


# ---- 6: failures keep the progress state ----------------------------------------------------------------------

@pytest.mark.parametrize("reply,finish,raises", [("SUMMARY", "length", False), ("", "stop", False), ("x", "stop", True)])
async def test_a_failed_summary_leaves_cursor_and_checkpoint(tmp_path, reply, finish, raises):
    c, session, provider, _ = make(tmp_path, turns=3, reply=reply, finish=finish)
    if raises:
        provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("boom"))
    session.metadata["_last_summary"] = {"text": "OLD", "last_active": session.updated_at.isoformat()}
    n = len(session.messages)
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    assert session.last_consolidated == 0 and len(session.messages) == n
    assert session.metadata["_last_summary"]["text"] == "OLD"


@pytest.mark.parametrize("mode", ["raises", "refused"])
async def test_a_failed_or_refused_session_save_puts_the_progress_back(tmp_path, mode):
    c, session, provider, _ = make(tmp_path, turns=3)
    session.metadata["_last_summary"] = {"text": "OLD", "last_active": session.updated_at.isoformat()}
    original_save = c.sessions.save

    def save(s, **kw):
        if mode == "raises":
            raise OSError("disk full")
        return False                                                   # a deleted/replaced generation refuses

    c.sessions.save = save
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    assert provider.chat_with_retry.await_count == 1                   # the summary was made ...
    assert session.last_consolidated == 0                              # ... but the originals stay unconsolidated
    assert session.metadata["_last_summary"]["text"] == "OLD"
    c.sessions.save = original_save
    assert c.sessions.get_or_create("cli:t").last_consolidated == 0


async def test_a_history_write_failure_keeps_the_originals(tmp_path, monkeypatch):
    c, session, provider, store = make(tmp_path, turns=3)
    calls = {"n": 0}
    real = store.append_history

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("history not writable")
        return real(*a, **kw)

    monkeypatch.setattr(store, "append_history", flaky)
    pressure(c)
    await c.maybe_consolidate_by_tokens(session)
    assert session.last_consolidated == 0


# ---- 9: repeated runs ----------------------------------------------------------------------------------------

async def test_repeated_runs_cover_every_message_in_order_without_gaps(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=9)
    set_budget(c, request_tokens(c, session, 2) + 20)                   # two turns per request
    covered_turns: list[int] = []
    for _ in range(8):
        before = session.last_consolidated
        c.estimate_session_prompt_tokens = MagicMock(side_effect=[(10**7, "t"), (0, "t")])
        await c.maybe_consolidate_by_tokens(session)
        if session.last_consolidated == before:
            break
        text = sent(provider, provider.chat_with_retry.await_count - 1)
        covered_turns += [n for n in range(9) if f"USER{n} " in text]
    assert covered_turns == sorted(set(covered_turns)) == list(range(8))        # in order, none skipped
    assert session.messages[session.last_consolidated]["content"].startswith("USER8 ")   # the latest turn stays live


async def test_an_oversized_turn_stops_every_later_run_without_calls(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=2)
    turn(session, 2, words=6000)                                         # the third turn is far over budget
    turn(session, 3)
    set_budget(c, request_tokens(c, session, 2) + 20)
    for _ in range(3):
        pressure(c)
        await c.maybe_consolidate_by_tokens(session)
    stopped_at = session.last_consolidated
    calls = provider.chat_with_retry.await_count
    for _ in range(3):
        pressure(c)
        await c.maybe_consolidate_by_tokens(session)
    assert session.last_consolidated == stopped_at and provider.chat_with_retry.await_count == calls
    assert session.messages[stopped_at]["content"].startswith("USER2 ")


# ---- 10: body-less messages -----------------------------------------------------------------------------------

async def test_a_message_without_text_but_with_tool_calls_or_attachments_is_not_empty(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=0)
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "exec"}}], "timestamp": "2026-01-01T00:00"},
            {"role": "user", "content": "", "media": ["a.png"], "timestamp": "2026-01-01T00:01"}]
    summary = await c.archive(msgs, session_key="cli:t", strict=True)
    assert summary == "SUMMARY" and provider.chat_with_retry.await_count == 1
    assert "tool calls: exec" in sent(provider) and "1 attachment(s)" in sent(provider)


async def test_a_range_with_nothing_in_it_is_done_without_a_call(tmp_path):
    c, _, provider, store = make(tmp_path, turns=0)
    assert await c.archive([{"role": "assistant", "content": ""}], session_key="cli:t", strict=True) == "(nothing)"
    assert provider.chat_with_retry.await_count == 0 and store._read_entries() == []


# ---- 7: idle compaction ---------------------------------------------------------------------------------------

async def test_idle_compaction_removes_only_what_was_summarized(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=12)
    set_budget(c, request_tokens(c, session, 3) + 20)                   # the removed part does not fit in one request
    result = await c.compact_idle_session("cli:t", max_suffix=4)
    assert result == "SUMMARY"
    after = c.sessions.get_or_create("cli:t")
    text = sent(provider)
    summarized = {n for n in range(12) if f"USER{n} " in text}
    live = {n for n in range(12) if any(m["content"].startswith(f"USER{n} ") for m in after.messages if m["role"] == "user")}
    assert summarized and live and summarized.isdisjoint(live)
    assert summarized | live == set(range(12))                           # nothing is in neither place
    assert after.last_consolidated == 0


async def test_idle_compaction_with_an_oversized_first_turn_changes_nothing(tmp_path):
    c, session, provider, store = make(tmp_path, turns=12)
    before = [dict(m) for m in session.messages]
    set_budget(c, request_tokens(c, session, 1) - 150)
    assert await c.compact_idle_session("cli:t", max_suffix=4) is None
    assert provider.chat_with_retry.await_count == 0 and store._read_entries() == []
    assert [dict(m) for m in c.sessions.get_or_create("cli:t").messages] == before


@pytest.mark.parametrize("mode", ["raises", "refused"])
async def test_idle_compaction_rolls_back_when_the_save_fails(tmp_path, mode):
    c, session, provider, _ = make(tmp_path, turns=12, words=30)
    before = [dict(m) for m in session.messages]

    def save(s, **kw):
        if mode == "raises":
            raise OSError("disk full")
        return False

    c.sessions.save = save
    assert await c.compact_idle_session("cli:t", max_suffix=4) is None
    live = c.sessions.get_or_create("cli:t")
    assert [dict(m) for m in live.messages] == before and "_last_summary" not in live.metadata


async def test_a_small_idle_session_is_compacted_as_before(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=12, words=20)
    assert await c.compact_idle_session("cli:t", max_suffix=4) == "SUMMARY"
    assert len(c.sessions.get_or_create("cli:t").messages) <= 4


# ---- /new keeps its legacy behaviour (out of scope here) -----------------------------------------------------

async def test_archive_without_strict_still_truncates_like_before(tmp_path):
    c, session, provider, _ = make(tmp_path, turns=0)
    set_budget(c, 400)
    big = [{"role": "user", "content": body("BIG", 4000), "timestamp": "2026-01-01T00:00"}]
    assert await c.archive(big, session_key="cli:t") == "SUMMARY"
    assert provider.chat_with_retry.await_count == 1 and "w3999" not in sent(provider)

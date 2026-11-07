"""Correction records reach every next-LLM input path (item 5): assembled execution prompt,
acquired DB evidence, memory tool output, session replay and session summaries."""
from __future__ import annotations

import json

import pytest

from nanobot.agent.context_corrections import (
    CORRECTION_EVENT_TYPE,
    CORRECTION_KIND,
    annotate_session,
    corrections_for,
)
from nanobot.agent.context_trace import ContextTrace
from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.memory import ReadMemoryEventsTool, SearchMemoryTool
from nanobot.session.manager import Session

from .test_context_obligations import GOLF_T2, T1_ANSWER, _old_turn, _wrapper
from .test_context_selection import event, need, plan, select
from .test_context_selection import env as env  # noqa: F401

WRAP_MARK = "부분 응답(미검증)"


def _dump(selected) -> str:
    # The executor prompt embeds execution_context() verbatim (loop.py selective branch).
    return json.dumps(json.loads(selected.execution_context()), ensure_ascii=False)


def _correct(env, original, answer=T1_ANSWER, session="old"):
    store, scope, *_ = env
    return store.append_event(
        scope=scope, conversation_id=session, session_id=session, actor="system",
        event_type=CORRECTION_EVENT_TYPE, content="verdict correction", parent_event_id=original.event_id,
        event_id="correction:" + original.event_id,
        metadata={"kind": CORRECTION_KIND, "original_status": "incomplete",
                  "corrected_status": "completed", "answer": answer})


def _tool(cls, tmp_path):
    tool = cls(str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="owner", metadata={"memory_owner_id": "owner"}))
    return tool


@pytest.mark.asyncio
async def test_follow_up_prompt_shows_latest_valid_state_after_reconcile(env):
    selector, _, reply = await _old_turn(env)
    assert selector.reconcile_verdicts(env[1], "s", ContextTrace(request_id="r2")) == 1
    event(env, GOLF_T2)
    _, _, selected = await select(env, plan(answer="ok"), request=GOLF_T2)
    text = _dump(selected)
    assert T1_ANSWER.splitlines()[0] in text and "corrected_complete" in text
    assert WRAP_MARK not in text and "요청을 완료하지 못했습니다" not in text
    # original record is kept unchanged in the store
    original = selector.store.get_events(env[1], [reply.event_id])[0]
    assert original.content.startswith("요청을 완료하지 못했습니다")
    # task status is the corrected one
    corrected = [t for t in selector.tasks.list(env[1], "s")
                 if any(c.get("kind") == "verdict_correction" for c in t.get("completed", []))]
    assert corrected and all(t["status"] == "DONE" and not t["unfinished"] for t in corrected)


@pytest.mark.asyncio
async def test_acquired_db_evidence_uses_correction(env):
    event(env, "골프 연습 60분 시간표 짜줘", session="old")
    wrapped = event(env, _wrapper(["시간표 60분"]), actor="assistant", session="old")
    _correct(env, wrapped)
    request = "골프 시간표 조사해줘"
    event(env, request)
    design = plan(route="retrieve", answer=None, information_needs=[need(
        query="골프 시간표")])
    _, _, selected = await select(env, design, request=request)
    shown = [r for r in selected.evidence if r["event_id"] == wrapped.event_id]
    assert shown and shown[0]["content"] == T1_ANSWER and shown[0]["verdict"] == "corrected_complete"
    assert shown[0]["correction_event_id"] == "correction:" + wrapped.event_id
    assert selected.trace.counters.get("corrections_applied") and "evidence" in selected.trace.reasons
    assert not [r for r in selected.evidence if r["event_id"].startswith("correction:")]
    assert env[0].get_events(env[1], [wrapped.event_id])[0].content == wrapped.content
    assert WRAP_MARK not in _dump(selected)


@pytest.mark.asyncio
async def test_memory_tools_append_latest_valid_state_and_keep_original(env, tmp_path):
    wrapped = event(env, _wrapper(["시간표 60분"]), actor="assistant", session="old")
    _correct(env, wrapped)
    read = await _tool(ReadMemoryEventsTool, tmp_path).execute(event_ids=[wrapped.event_id])
    read = read if isinstance(read, str) else str(getattr(read, "content", read))
    assert WRAP_MARK in read  # original verbatim
    head, notes = read.split("Corrections (latest valid state", 1)
    assert WRAP_MARK in head and T1_ANSWER in notes and "incomplete -> completed" in notes
    found = await _tool(SearchMemoryTool, tmp_path).execute(query="퍼팅 어프로치")
    assert "Corrections (latest valid state" in found and T1_ANSWER in found


@pytest.mark.asyncio
async def test_memory_tools_unchanged_without_correction(env, tmp_path):
    plain = event(env, "그냥 답변 퍼팅", actor="assistant", session="old")
    read = await _tool(ReadMemoryEventsTool, tmp_path).execute(event_ids=[plain.event_id])
    read = read if isinstance(read, str) else str(getattr(read, "content", read))
    assert "Corrections" not in read
    assert "Corrections" not in await _tool(SearchMemoryTool, tmp_path).execute(query="퍼팅")


def test_session_replay_and_summary_use_correction(env):
    wrapped = event(env, _wrapper(["시간표 60분"]), actor="assistant", session="s")
    _correct(env, wrapped, session="s")
    session = Session(key="cli:s", messages=[
        {"role": "user", "content": "골프 연습 시간표", "timestamp": "2026-10-28T10:00"},
        {"role": "assistant", "content": wrapped.content, "memory_event_id": wrapped.event_id,
         "timestamp": "2026-10-28T10:01"}])
    assert annotate_session(env[0], env[1], session) == 1
    assert annotate_session(env[0], env[1], session) == 0  # idempotent
    assert session.messages[1]["content"] == wrapped.content  # original kept
    replay = session.get_history(max_messages=10)
    assert replay[-1]["content"] == T1_ANSWER
    summary_input = MemoryStore._format_messages(session.messages)
    assert "[corrected: corrected_complete]" in summary_input and WRAP_MARK not in summary_input


def test_session_without_correction_is_untouched(env):
    reply = event(env, "평범한 답", actor="assistant", session="s")
    session = Session(key="cli:s", messages=[
        {"role": "assistant", "content": "평범한 답", "memory_event_id": reply.event_id}])
    assert annotate_session(env[0], env[1], session) == 0
    assert "correction" not in session.messages[0]
    assert session.get_history(max_messages=10)[-1]["content"] == "평범한 답"


def test_corrections_are_scope_bound(env):
    from nanobot.memory.models import MemoryScope
    wrapped = event(env, _wrapper(["x"]), actor="assistant", session="s")
    _correct(env, wrapped, session="s")
    other = MemoryScope.from_runtime(owner_id="someone-else", workspace_id=env[1].workspace_ids[0], agent_id=None)
    assert corrections_for(env[0], env[1], [wrapped.event_id])
    assert corrections_for(env[0], other, [wrapped.event_id]) == {}


def _seed_corrected_session(loop):
    from nanobot.bus.events import InboundMessage
    session = loop.sessions.get_or_create("cli:s")
    wrapped = _wrapper(["시간표 60분"])
    session.messages += [
        {"role": "user", "content": "골프 연습 60분 시간표 짜줘", "timestamp": "2026-10-28T10:00:00",
         "memory_event_id": "Eseed_user"},
        {"role": "assistant", "content": wrapped, "timestamp": "2026-10-28T10:01:00",
         "memory_event_id": "Eseed_answer"}]
    loop.sessions.save(session)
    scope = loop._memory_scope_for_message(InboundMessage(channel="cli", sender_id="owner", chat_id="s", content=""))
    loop.conversation_memory.insert_events(loop._memory_events_from_session_messages(
        msg=InboundMessage(channel="cli", sender_id="owner", chat_id="s", content=""), session=session,
        messages=session.messages, start_index=0))
    original = loop.conversation_memory.get_events(scope, ["Eseed_answer"])[0]
    loop.conversation_memory.append_event(
        scope=scope, conversation_id="cli:s", session_id="cli:s", actor="system",
        event_type=CORRECTION_EVENT_TYPE, content="verdict correction", parent_event_id=original.event_id,
        event_id="correction:" + original.event_id,
        metadata={"kind": CORRECTION_KIND, "original_status": "incomplete",
                  "corrected_status": "completed", "answer": T1_ANSWER})
    return wrapped


@pytest.mark.asyncio
async def test_loop_design_prompt_and_legacy_replay_carry_correction(tmp_path):
    from unittest.mock import patch

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ContextSelectionConfig
    from nanobot.providers.base import LLMResponse

    from .test_context_selection import FakeProvider
    provider = FakeProvider([LLMResponse(content="invalid"), LLMResponse(content="invalid"),
                             LLMResponse(content="Legacy recovery answer")])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True, failure_mode="legacy"))
    wrapped = _seed_corrected_session(loop)
    with patch.object(loop.consolidator, "maybe_consolidate_by_tokens", return_value=None):
        out = await loop._process_message(InboundMessage(
            channel="cli", sender_id="owner", chat_id="s", content="방금 시간표를 45분으로 줄여 줘"))
    assert out.content == "Legacy recovery answer"
    design_input = json.dumps(provider.requests[0]["messages"], ensure_ascii=False)
    assert T1_ANSWER.splitlines()[0] in design_input and WRAP_MARK not in design_input
    replay = json.dumps(provider.requests[-1]["messages"], ensure_ascii=False)
    assert T1_ANSWER.splitlines()[0] in replay and WRAP_MARK not in replay
    stored = next(m for m in loop.sessions.get_or_create("cli:s").messages
                  if m.get("memory_event_id") == "Eseed_answer")
    assert stored["content"] == wrapped and stored["correction"]["answer"] == T1_ANSWER

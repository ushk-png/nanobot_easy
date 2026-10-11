"""Fixes from Genie's 12:43 run (correction message, 17 calls, wrong 'deferred' answer)."""

from __future__ import annotations

from typing import Any

from nanobot.reasoning.checks import condition_covered
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from nanobot.reasoning.models import ReasoningCase
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS

CONVERSATION = [
    {"role": "user", "content": "A: 외부 통신 없음, 감사 로그 없음, 하루 1,500건, 4,000만 원 ..."},
    {"role": "assistant", "content": "C를 선정합니다."},
]


def test_fact_step_receives_the_conversation() -> None:
    """S2 only saw the correction, not the A-D table, and deferred the choice."""

    seen: dict[str, Any] = {}

    class Spy(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            seen[stage] = payload
            return super().complete(stage, payload)

    ReasoningEngine(Spy(dict(STAGE_OUTPUTS))).run("C는 950건입니다.", conversation=CONVERSATION)

    assert seen["S1"]["conversation"] == CONVERSATION
    assert seen["S2"]["conversation"] == CONVERSATION
    assert "conversation" not in seen["S4"]


def test_condition_check_accepts_a_faithful_paraphrase() -> None:
    """S7 stopped because the condition was not copied verbatim."""

    condition = "외부통신·감사로그·처리량·도입비가 확인되어야 한다."
    paraphrase = "외부통신·감사로그·처리량·도입비가 모두 확인되어야 선정할 수 있습니다."
    partial = "외부통신·처리량·도입비가 확인되어야 선정할 수 있습니다."

    assert condition_covered(condition, paraphrase)
    assert condition_covered(condition, partial)  # 4 of 5 terms
    assert not condition_covered(condition, "C를 선정합니다.")
    assert not condition_covered("조건A", "조건 없이 답합니다.")


def test_answer_that_failed_its_last_check_is_labelled() -> None:
    from nanobot.reasoning.bridge import user_reply

    stopped = ReasoningCase(
        id="c", request="r", procedure_id="method-evaluation", procedure_version="3.1",
        status="stopped", answer="C를 선정합니다.", stop_reason="answer omits condition: x",
    )
    completed = stopped.model_copy(update={"status": "completed"})
    waiting = stopped.model_copy(update={"status": "waiting_info", "answer": "", "stop_reason": "어느 제품?"})

    assert user_reply(completed) == "C를 선정합니다."
    assert user_reply(stopped).startswith("C를 선정합니다.")
    assert "마지막 점검을 통과하지 못했습니다" in user_reply(stopped)
    assert user_reply(waiting) == "어느 제품?"


def test_stepwise_reply_is_sent_on_streaming_channels(monkeypatch) -> None:
    """Observed live: Telegram (streaming on) never received stepwise final answers.

    The reply was marked StreamedResponseEvent although nothing had been streamed,
    and the channel manager skips such messages as already delivered.
    """

    import asyncio
    from types import SimpleNamespace

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage

    async def _fake_run(*_args, **_kwargs):
        return ReasoningCase(
            id="c", request="r", procedure_id="constraint-selection", procedure_version="3.1",
            status="completed", answer="C를 선정합니다.",
        )

    class _Runtime:
        async def run_status_changed(self, *_args, **_kwargs) -> None:
            return None

    async def _stream(_delta: str) -> None:
        return None

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _fake_run)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = "none"
    loop.workspace = None
    loop.provider = object()
    loop.model = "m"
    loop.tools = {}
    loop._runtime_events = lambda: _Runtime()
    msg = InboundMessage(channel="telegram", sender_id="u", chat_id="1", content="선정해 주세요 A B C D")
    ctx = SimpleNamespace(
        msg=msg, session_key="telegram:1", visible_run_started_at=None, initial_messages=[],
        final_content=None, tools_used=None, all_messages=None, stop_reason=None,
        had_injections=None, on_progress=None, on_stream=_stream,
    )

    asyncio.run(loop._state_run(ctx))
    outbound = loop._assemble_outbound(
        msg, ctx.final_content, ctx.all_messages, ctx.stop_reason, ctx.had_injections, ctx.on_stream
    )

    assert outbound is not None
    assert outbound.content.endswith("C를 선정합니다.")
    assert outbound.event is None

"""Genie-only routing into the code-controlled method evaluation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nanobot.agent.loop import AgentLoop
from nanobot.reasoning.bridge import is_method_evaluation_request, select_procedure


class _FakeRuntime:
    async def run_status_changed(self, *_args, **_kwargs) -> None:
        return None


def test_stage_prompt_keeps_intermediate_judgments_short() -> None:
    from nanobot.reasoning.bridge import ProviderStageLLM

    class _Provider:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def chat_with_retry(self, **kwargs):
            self.messages = kwargs["messages"]

            class _Response:
                content = "{}"
                usage = {}

            return _Response()

    provider = _Provider()
    import asyncio

    asyncio.run(ProviderStageLLM(provider, "test")._complete("S2", {"stage": "S2"}))
    prompt = provider.messages[0]["content"]

    assert "짧은 판단" in prompt
    assert "2~4문장" in prompt
    assert "그 외 단계의 answer는 비운다" in prompt


def test_method_evaluation_marker_is_narrow() -> None:
    assert is_method_evaluation_request("이 방법이 적합한가요?")
    assert select_procedure("장애 원인 분석해 줘") == "cause-analysis"
    assert select_procedure("이 문제 해결 방법은?") == "problem-solving"
    assert select_procedure("보고서 검토해 줘") == "document-review"
    assert select_procedure("규정에 따라 판단해 줘") == "rule-judgment"
    assert select_procedure("오늘 날씨 알려줘") is None
    assert select_procedure("원인 분석하고 문서 검토도 해 줘") is None


def test_disabled_reasoning_routes_method_evaluation(monkeypatch) -> None:
    async def _fake_run(*_args, **_kwargs):
        from nanobot.reasoning.models import ReasoningCase

        return ReasoningCase(
            id="case",
            request="평가",
            procedure_id="method-evaluation",
            procedure_version="3.1",
            status="completed",
            answer="조건부입니다.",
        )

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _fake_run)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = "none"
    loop.workspace = None
    loop.provider = object()
    loop.model = "xai-grok/grok-4.7"
    loop._runtime_events = lambda: _FakeRuntime()
    ctx = SimpleNamespace(
        msg=SimpleNamespace(content="이 방법이 적합한가요?"),
        session_key="telegram:test",
        visible_run_started_at=None,
        initial_messages=[{"role": "user", "content": "이 방법이 적합한가요?"}],
        final_content=None,
        tools_used=None,
        all_messages=None,
        stop_reason=None,
        had_injections=None,
        on_progress=None,
    )

    assert asyncio.run(loop._state_run(ctx)) == "ok"
    assert ctx.final_content == "조건부입니다."
    assert ctx.stop_reason == "completed"


def test_enabled_reasoning_keeps_normal_loop(monkeypatch) -> None:
    called = {"value": False}

    async def _normal(*_args, **_kwargs):
        called["value"] = True
        return "answer", [], [], "completed", False

    monkeypatch.setattr(AgentLoop, "_run_agent_loop", _normal)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = None
    loop._runtime_events = lambda: _FakeRuntime()
    ctx = SimpleNamespace(
        msg=SimpleNamespace(content="이 방법이 적합한가요?", channel="telegram", chat_id="1", metadata={}),
        session_key="telegram:test",
        visible_run_started_at=1,
        initial_messages=[],
        on_progress=None,
        on_stream=None,
        on_stream_end=None,
        on_retry_wait=None,
        session=None,
        pending_queue=None,
        ephemeral=False,
        run_extra_hooks_for_ephemeral=False,
        hooks=[],
        tools=None,
    )

    assert asyncio.run(loop._state_run(ctx)) == "ok"
    assert called["value"] is True

"""Reasoning that cannot be turned off is marked, not hidden (design 1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nanobot.providers.xai_grok_provider import XAIGrokProvider
from nanobot.workflow.llm_adapter import WorkflowLLMAdapter
from tests.reasoning.test_stepwise_instrumentation import FakeProvider, _run


def test_xai_always_on_models_report_no_disable_support() -> None:
    assert XAIGrokProvider.reasoning_disable_supported("xai-grok/grok-4.7") is False
    assert XAIGrokProvider.reasoning_disable_supported("grok-4.5") is False
    assert XAIGrokProvider.reasoning_disable_supported("xai-grok/grok-3-mini") is None
    assert XAIGrokProvider.reasoning_request_options("none") is None


def test_unsupported_reasoning_off_is_recorded_on_the_run(tmp_path: Path) -> None:
    class AlwaysOn(FakeProvider):
        @staticmethod
        def reasoning_disable_supported(model: str | None) -> bool | None:
            del model
            return False

    _run(AlwaysOn(), tmp_path)

    record = json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])
    mode = record["reasoning_mode"]
    assert mode["requested_effort"] == "low"
    assert mode["disable_supported"] is False
    assert mode["status"].startswith("unsupported")


def test_unknown_support_is_not_reported_as_supported(tmp_path: Path) -> None:
    _run(FakeProvider(), tmp_path)

    record = json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])
    assert record["reasoning_mode"]["disable_supported"] is None
    assert record["reasoning_mode"]["status"].startswith("unknown")


def test_workflow_call_json_passes_the_configured_reasoning_effort() -> None:
    class Provider:
        generation = SimpleNamespace(reasoning_effort="low")

        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def chat(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(content="{}", finish_reason="stop")

    provider = Provider()
    asyncio.run(WorkflowLLMAdapter(provider).call_json(system="s", payload={}))
    assert provider.kwargs["reasoning_effort"] == "low"

    asyncio.run(
        WorkflowLLMAdapter(provider).call_json(system="s", payload={}, reasoning_effort="high")
    )
    assert provider.kwargs["reasoning_effort"] == "high"


def _loop_with(monkeypatch, events: list[str]):
    from nanobot.agent.loop import AgentLoop
    from nanobot.reasoning.models import ReasoningCase

    async def _fake_run(*_args, **_kwargs):
        events.append("run")
        return ReasoningCase(
            id="case",
            request="r",
            procedure_id="method-evaluation",
            procedure_version="3.1",
            status="completed",
            answer="답입니다.",
        )

    class _Runtime:
        async def run_status_changed(self, *_args, **_kwargs) -> None:
            return None

    class _AlwaysOn:
        @staticmethod
        def reasoning_disable_supported(model):
            del model
            return False

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _fake_run)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = "none"
    loop.workspace = None
    loop.provider = _AlwaysOn()
    loop.model = "xai-grok/grok-4.7"
    loop._runtime_events = lambda: _Runtime()
    return loop


def _ctx(on_progress=None, session_key: str = "telegram:1"):
    return SimpleNamespace(
        msg=SimpleNamespace(content="이 방법이 내부망 조건에 적합한지 평가해 주세요."),
        session_key=session_key,
        visible_run_started_at=None,
        initial_messages=[],
        final_content=None,
        tools_used=None,
        all_messages=None,
        stop_reason=None,
        had_injections=None,
        on_progress=on_progress,
    )


def test_unsupported_reasoning_off_is_announced_before_the_run_once_per_session(monkeypatch) -> None:
    events: list[str] = []
    loop = _loop_with(monkeypatch, events)

    async def progress(content: str, *, tool_hint: bool = False) -> None:
        del tool_hint
        events.append(content)

    asyncio.run(loop._state_run(_ctx(progress)))
    asyncio.run(loop._state_run(_ctx(progress)))

    notices = [item for item in events if item.startswith("안내:")]
    assert len(notices) == 1
    assert events.index(notices[0]) < events.index("run")
    assert "추론을 끌 수 없어" in notices[0]


def test_notice_is_prefixed_when_the_channel_has_no_progress(monkeypatch) -> None:
    loop = _loop_with(monkeypatch, [])
    ctx = _ctx(None)

    asyncio.run(loop._state_run(ctx))

    assert ctx.final_content.startswith("안내:")
    assert ctx.final_content.endswith("답입니다.")


def test_stage_compose_and_review_calls_record_the_reasoning_actually_sent() -> None:
    """The record shows the body's reasoning field when the provider reports it."""

    from nanobot.reasoning.bridge import ProviderStageLLM

    sent = {"summary": "concise", "effort": "low"}

    class Provider:
        def __init__(self) -> None:
            self.efforts: list[Any] = []

        async def chat_with_retry(self, **kwargs):
            self.efforts.append(kwargs["reasoning_effort"])
            return SimpleNamespace(
                content="{}", usage={}, finish_reason="stop", request_options={"reasoning": sent}
            )

    provider = Provider()
    llm = ProviderStageLLM(provider, "m")
    for stage in ("procedure", "review", "S1"):
        llm.complete(stage, {})

    assert provider.efforts == ["low", "low", "low"]
    assert [item["call_kind"] for item in llm.call_records] == ["compose", "review", "step"]
    for record in llm.call_records:
        assert record["reasoning_request"] == sent
        assert record["reasoning_request_source"] == "request"


def test_xai_response_reports_the_reasoning_field_from_the_final_body(monkeypatch) -> None:
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "nanobot.providers.xai_grok_provider.get_xai_oauth_token",
        lambda **_kwargs: SimpleNamespace(access="t", account_id=None),
    )

    async def fake_request(_url, _headers, body, **_kwargs):
        bodies.append(body)
        return "{}", [], "stop", {}, None

    monkeypatch.setattr("nanobot.providers.xai_grok_provider._request_xai", fake_request)
    provider = XAIGrokProvider(default_model="xai-grok/grok-4.7", hosted_search=False)

    low = asyncio.run(provider.chat([{"role": "user", "content": "hi"}], reasoning_effort="low"))
    none = asyncio.run(provider.chat([{"role": "user", "content": "hi"}], reasoning_effort="none"))

    assert low.request_options == {"reasoning": bodies[0]["reasoning"]}
    assert bodies[0]["reasoning"]["effort"] == "low"
    assert none.request_options == {"reasoning": None}


def test_agent_loop_forwards_the_z3_setting(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    loop = _loop_with(monkeypatch, [])

    async def _capture(*_args, **kwargs):
        from nanobot.reasoning.models import ReasoningCase

        seen.update(kwargs)
        return ReasoningCase(
            id="c", request="r", procedure_id="p", procedure_version="3.1",
            status="completed", answer="a",
        )

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _capture)
    asyncio.run(loop._state_run(_ctx(None)))
    assert seen["z3_check"] is False

    loop.stepwise_z3_check = True
    asyncio.run(loop._state_run(_ctx(None, session_key="telegram:2")))
    assert seen["z3_check"] is True

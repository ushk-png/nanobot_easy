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
    assert mode["requested_effort"] == "none"
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

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

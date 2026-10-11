"""Stage calls carry no hosted tools; model output cannot steer the revision guard."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.providers.base import hosted_tools_disabled, hosted_tools_enabled
from nanobot.providers.xai_grok_provider import XAIGrokProvider
from nanobot.reasoning.bridge import ProviderStageLLM
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS


def _mock_xai(monkeypatch, *, supports_backend_search: bool = True) -> list[dict[str, Any]]:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "nanobot.providers.xai_grok_provider.get_xai_oauth_token",
        lambda **_kwargs: SimpleNamespace(access="t", account_id=None),
    )

    async def capabilities(*_args, **_kwargs):
        return {"grok-4.7": supports_backend_search}

    bodies: list[dict[str, Any]] = []

    async def fake_request(_url, _headers, body, **_kwargs):
        bodies.append(body)
        return '{"goal": "g"}', [], "stop", {}, None

    monkeypatch.setattr(
        "nanobot.providers.xai_grok_provider._fetch_xai_model_capabilities", capabilities
    )
    monkeypatch.setattr("nanobot.providers.xai_grok_provider._request_xai", fake_request)
    return bodies


def test_hosted_tools_flag_is_scoped() -> None:
    assert hosted_tools_enabled() is True
    with hosted_tools_disabled():
        assert hosted_tools_enabled() is False
    assert hosted_tools_enabled() is True


@pytest.mark.asyncio
async def test_toolless_request_omits_tool_choice(monkeypatch) -> None:
    bodies = _mock_xai(monkeypatch)
    provider = XAIGrokProvider(default_model="xai-grok/grok-4.7", hosted_search=False)

    await provider.chat([{"role": "user", "content": "hi"}])

    assert "tool_choice" not in bodies[0]
    assert "tools" not in bodies[0]


@pytest.mark.asyncio
async def test_hosted_search_is_not_attached_inside_disabled_block(monkeypatch) -> None:
    bodies = _mock_xai(monkeypatch)
    provider = XAIGrokProvider(default_model="xai-grok/grok-4.7", hosted_search=True)

    await provider.chat([{"role": "user", "content": "hi"}])
    with hosted_tools_disabled():
        await provider.chat([{"role": "user", "content": "hi"}])

    assert {"type": "x_search"} in bodies[0]["tools"]
    assert "tools" not in bodies[1]


def test_stage_calls_carry_no_hosted_tools(monkeypatch) -> None:
    bodies = _mock_xai(monkeypatch)
    provider = XAIGrokProvider(default_model="xai-grok/grok-4.7", hosted_search=True)

    ProviderStageLLM(provider, "xai-grok/grok-4.7").complete("S1", {"stage": "S1"})

    assert "tools" not in bodies[0]
    assert "tool_choice" not in bodies[0]


def test_model_written_revision_is_dropped_by_the_adapter() -> None:
    class Provider:
        async def chat_with_retry(self, **_kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                content='{"goal": "g", "revision": "v2"}', usage={}, finish_reason="stop"
            )

    result = ProviderStageLLM(Provider(), "m").complete("S1", {"stage": "S1"})

    assert "revision" not in result
    assert result["goal"] == "g"


def test_malformed_revision_tag_is_stale_not_a_crash() -> None:
    class BadTag(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            result = super().complete(stage, payload)
            result["revision"] = "v2"
            return result

    case = ReasoningEngine(BadTag(dict(STAGE_OUTPUTS))).run("평가")

    assert case.status == "stopped"
    assert case.stop_reason == "stale revision result repeated"


def test_stage_call_runs_in_a_worker_thread_event_loop() -> None:
    """ProviderStageLLM is called via asyncio.to_thread; the scoped flag must hold there too."""

    seen: list[bool] = []

    class Provider:
        async def chat_with_retry(self, **_kwargs):
            from types import SimpleNamespace

            seen.append(hosted_tools_enabled())
            return SimpleNamespace(content="{}", usage={}, finish_reason="stop")

    async def run() -> None:
        await asyncio.to_thread(ProviderStageLLM(Provider(), "m").complete, "S1", {})

    asyncio.run(run())
    assert seen == [False]

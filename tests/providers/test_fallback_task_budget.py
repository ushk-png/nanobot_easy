"""Item 2: the whole-request TaskBudget counts real sends only, not the FallbackProvider wrapper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.agent.context_trace import ContextTrace, active_context_trace
from nanobot.agent.task_budget import TaskBudget
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.fallback_provider import FallbackProvider

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class Fake(LLMProvider):
    def __init__(self, ok: bool = True):
        super().__init__(api_key="x")
        self.ok = ok
        self.real_sends = 0

    def _reply(self) -> LLMResponse:
        self.real_sends += 1
        if self.ok:
            return LLMResponse(content="hi", finish_reason="stop", usage=dict(USAGE))
        return LLMResponse(content="Error calling LLM: boom", finish_reason="error",
                           error_status_code=503, error_should_retry=True)

    async def chat(self, **kwargs):
        return self._reply()

    async def chat_stream(self, **kwargs):
        return self._reply()

    def get_default_model(self) -> str:
        return "fake"


def _preset():
    return SimpleNamespace(model="fb", max_tokens=100, temperature=0.0, reasoning_effort=None)


async def _send(provider, budget, stream):
    trace = ContextTrace(request_id="t")
    trace.budget = budget
    token = active_context_trace.set(trace)
    try:
        call = provider._safe_chat_stream if stream else provider._safe_chat
        return await call(messages=[{"role": "user", "content": "x"}], model="fake")
    finally:
        active_context_trace.reset(token)


@pytest.mark.parametrize("stream", [False, True])
async def test_primary_success_counts_one_send(stream):
    primary, fb = Fake(True), Fake(True)
    budget = TaskBudget("t", max_sends=4)
    await _send(FallbackProvider(primary, [_preset()], lambda _: fb), budget, stream)
    assert primary.real_sends + fb.real_sends == 1
    assert budget.sends == 1 and budget.tokens_known == 15


@pytest.mark.parametrize("stream", [False, True])
async def test_fallback_after_failure_counts_real_sends(stream):
    primary, fb = Fake(False), Fake(True)
    budget = TaskBudget("t", max_sends=4)
    response = await _send(FallbackProvider(primary, [_preset()], lambda _: fb), budget, stream)
    assert response.finish_reason == "stop"
    assert primary.real_sends == 1 and fb.real_sends == 1
    assert budget.sends == 2 and budget.tokens_known == 15
    assert budget.unknown_usage == 1   # the failed primary reported no usage (unknown, not 0)


@pytest.mark.parametrize("stream", [False, True])
async def test_exhausted_budget_does_not_try_the_fallback(stream):
    primary, fb = Fake(False), Fake(True)
    budget = TaskBudget("t", max_sends=1)
    response = await _send(FallbackProvider(primary, [_preset()], lambda _: fb), budget, stream)
    assert primary.real_sends == 1 and fb.real_sends == 0
    assert response.error_kind == "budget_exhausted"
    assert budget.sends == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_plain_provider_still_counts_once(stream):
    plain = Fake(True)
    budget = TaskBudget("t", max_sends=4)
    await _send(plain, budget, stream)
    assert budget.sends == 1 and budget.tokens_known == 15


def test_only_the_wrapper_skips_budget_counting():
    assert LLMProvider.counts_task_budget is True
    assert FallbackProvider.counts_task_budget is False

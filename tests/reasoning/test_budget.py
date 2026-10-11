"""Per-run time and call budget (approval item 6)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.reasoning.bridge import ProviderStageLLM, run_reasoning_procedure, user_reply
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import BudgetExceededError, RunBudget, ScriptedStageLLM
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS


def test_call_budget_counts_every_call() -> None:
    budget = RunBudget(seconds=None, max_calls=2)
    budget.before_call("a")
    budget.before_call("b")

    with pytest.raises(BudgetExceededError) as exc:
        budget.before_call("c")
    assert exc.value.kind == "calls"


def test_time_budget_cuts_a_request_in_flight() -> None:
    class Slow:
        async def chat_with_retry(self, **_kwargs):
            await asyncio.sleep(5)
            return SimpleNamespace(content="{}", usage={}, finish_reason="stop")

    llm = ProviderStageLLM(Slow(), "m")
    llm.budget = RunBudget(seconds=0.2, max_calls=8)
    started = time.monotonic()

    with pytest.raises(BudgetExceededError) as exc:
        llm.complete("S1", {})

    assert exc.value.kind == "time"
    assert time.monotonic() - started < 2
    assert llm.call_records[-1]["finish_reason"] == "budget_timeout"


def test_engine_keeps_confirmed_results_when_the_budget_runs_out() -> None:
    class Limited(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            if stage == "S5":
                raise BudgetExceededError("calls", "8 model calls used before S5")
            return super().complete(stage, payload)

    case = ReasoningEngine(Limited(dict(STAGE_OUTPUTS))).run("평가")

    assert case.status == "incomplete"
    assert case.step_states["S4"] == "passed"
    assert case.step_states["S5"] == "stopped"
    reply = user_reply(case)
    assert "호출 횟수 예산" in reply
    assert "충족" in reply  # the confirmed judgment is shown
    assert "미완료 단계" in reply and "S5" in reply


def test_routing_call_over_budget_is_incomplete(tmp_path) -> None:
    class Provider:
        async def chat_with_retry(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({"mode": "direct"}), usage={}, finish_reason="stop")

    case = asyncio.run(
        run_reasoning_procedure(
            Provider(), model="m", request="안녕", workspace=tmp_path, call_budget=0
        )
    )

    # Default: fall back to the general path instead of ending with nothing (design 4.5).
    assert case.status == "direct" and case.path == "fallback"
    assert {"event": "budget_exceeded", "stage": "procedure", "kind": "calls"} in case.history

    stopped = asyncio.run(
        run_reasoning_procedure(
            Provider(), model="m", request="안녕", workspace=tmp_path, call_budget=0,
            fallback_direct=False,
        )
    )
    assert stopped.status == "incomplete"
    assert "예산" in user_reply(stopped)

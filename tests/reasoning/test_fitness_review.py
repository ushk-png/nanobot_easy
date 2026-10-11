"""Fitness review is a separate call before a new procedure runs (design 4.4, 10)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nanobot.reasoning.bridge import _PROCEDURE_PROMPT, _REVIEW_PROMPT, run_reasoning_procedure

REQUEST = "후보 보고서 세 개 중 하나를 근거와 함께 골라 주세요. 조건: 외부 통신 없음."

# Checks candidates but never picks one or gives reasons.
INCOMPLETE = {
    "purpose": "후보 보고서의 조건 충족을 확인한다.",
    "steps": [
        {"id": "check", "question": "후보별 조건 충족을 판정한다.", "outputs": ["checked"]},
        {"id": "report", "role": "answer", "question": "판정 결과를 답한다.", "inputs": ["check"], "outputs": ["answer"]},
    ],
}
COMPLETE = {
    "purpose": "조건에 맞는 보고서 하나를 근거와 함께 고른다.",
    "steps": [
        {"id": "check", "question": "후보별 조건 충족을 판정한다.", "outputs": ["checked"]},
        {"id": "pick", "question": "하나를 고른다.", "inputs": ["check"], "outputs": ["selection"]},
        {"id": "justify", "role": "answer", "question": "선정과 근거를 답한다.", "inputs": ["pick"], "outputs": ["answer"]},
    ],
}


def _review_for(procedure: dict[str, Any]) -> dict[str, Any]:
    """A reviewer that maps request items onto the steps it actually sees."""

    ids = {step["id"] for step in procedure["steps"]}
    return {
        "request_items": [
            {"item": "조건 확인", "step_id": "check"},
            {"item": "하나를 고르기", "step_id": "pick" if "pick" in ids else None},
            {"item": "근거 제시", "step_id": "justify" if "justify" in ids else None},
        ],
        "answer_step_id": "justify" if "justify" in ids else "report",
        "defects": [],
    }


class ReviewingProvider:
    """Routes by system prompt: compose, review, or a step call."""

    def __init__(self, proposals: list[dict[str, Any]]) -> None:
        self.proposals = list(proposals)
        self.last_proposal: dict[str, Any] = {}
        self.kinds: list[str] = []
        self.review_inputs: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        body = json.loads(kwargs["messages"][1]["content"])
        if system == _REVIEW_PROMPT:
            self.kinds.append("review")
            self.review_inputs.append(body)
            content = _review_for(self.last_proposal)
        elif system == _PROCEDURE_PROMPT:
            self.kinds.append("compose")
            self.last_proposal = self.proposals.pop(0) if len(self.proposals) > 1 else self.proposals[0]
            content = self.last_proposal
        else:
            self.kinds.append("step")
            outputs = body.get("expected_outputs") or []
            content = {
                key: ("보고서 B를 고릅니다. 외부 통신이 없습니다." if key == "answer" else "결과")
                for key in outputs
            }
        return SimpleNamespace(
            content=json.dumps(content, ensure_ascii=False),
            usage={"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 2},
            finish_reason="stop",
        )


def _run(provider: ReviewingProvider, tmp_path: Path, procedure_id: str | None = None):
    return asyncio.run(
        run_reasoning_procedure(
            provider,
            model="xai-grok/grok-4.7",
            request=REQUEST,
            procedure_id=procedure_id,
            workspace=tmp_path,
        )
    )


def test_review_catches_a_procedure_that_omits_the_requested_choice(tmp_path: Path) -> None:
    provider = ReviewingProvider([INCOMPLETE, COMPLETE])

    case = _run(provider, tmp_path)

    assert provider.kinds[:4] == ["compose", "review", "compose", "review"]
    assert case.status == "completed"
    assert "justify" in case.step_states
    record = json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])
    kinds = [item["call_kind"] for item in record["calls"]]
    assert kinds.count("review") == 2
    assert kinds[-1] == "answer"


def test_review_input_is_request_and_procedure_only(tmp_path: Path) -> None:
    provider = ReviewingProvider([COMPLETE])

    _run(provider, tmp_path)

    review_input = provider.review_inputs[0]
    assert set(review_input) == {"request", "known_goal", "procedure"}
    assert review_input["request"] == REQUEST
    assert [step["id"] for step in review_input["procedure"]["steps"]] == ["check", "pick", "justify"]


def test_procedure_that_never_passes_review_is_not_executed(tmp_path: Path) -> None:
    provider = ReviewingProvider([INCOMPLETE])

    case = _run(provider, tmp_path)

    assert "step" not in provider.kinds
    # One review-driven repair, then the general path answers (design 4.5).
    assert case.status == "direct" and case.path == "fallback"
    assert {"event": "fallback_direct", "reason": "review"} in case.history
    record = json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])
    assert any(item.startswith("review_unhandled") for item in record["defects"])
    assert [item["call_kind"] for item in record["calls"]] == ["compose", "review"] * 2

    provider = ReviewingProvider([INCOMPLETE])
    stopped = asyncio.run(
        run_reasoning_procedure(
            provider, model="xai-grok/grok-4.7", request=REQUEST, workspace=tmp_path, fallback_direct=False
        )
    )
    assert stopped.status == "stopped"
    assert "실행하지 않았습니다" in stopped.stop_reason


def test_reviewer_without_mapping_does_not_pass(tmp_path: Path) -> None:
    from nanobot.reasoning.bridge import review_defects
    from nanobot.reasoning.dynamic import validate_dynamic_procedure

    procedure, _ = validate_dynamic_procedure(COMPLETE, request=REQUEST)
    assert procedure is not None

    assert review_defects({}, procedure)[0].startswith("review_unusable")
    assert review_defects(
        {"request_items": [{"item": "고르기", "step_id": "pick"}], "answer_step_id": "pick"},
        procedure,
    ) == ["review_no_answer_step: no step writes the final answer"]


def test_approved_procedure_skips_review(tmp_path: Path) -> None:
    from tests.reasoning.test_stepwise_instrumentation import FakeProvider

    case = _run(FakeProvider(), tmp_path, procedure_id="method-evaluation")

    kinds = {item["call_kind"] for item in case.call_records}
    assert case.status == "completed"
    assert kinds == {"step", "answer"}


def test_composition_stops_when_too_little_budget_is_left_to_run(tmp_path: Path) -> None:
    """Observed live: three compose/review rounds used 120 s and S1 never ran."""

    provider = ReviewingProvider([INCOMPLETE, COMPLETE])

    case = asyncio.run(
        run_reasoning_procedure(
            provider, model="m", request=REQUEST, workspace=tmp_path, time_budget_s=None, call_budget=4
        )
    )

    # Round one used 2 of 4 calls; 2 left is below the 3 kept for execution.
    assert provider.kinds == ["compose", "review"]
    assert case.status == "direct" and case.path == "fallback"
    assert {"event": "fallback_direct", "reason": "budget"} in case.history


def test_agent_loop_tells_the_user_before_falling_back(monkeypatch) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.reasoning.bridge import FALLBACK_NOTICE, _fallback_case

    notes: list[str] = []

    async def _fake_run(*_args, **_kwargs):
        return _fallback_case("r", "review")

    async def _normal(*_args, **_kwargs):
        return "일반 답", [], [], "completed", False

    async def _continue(_ctx) -> None:
        return None

    async def _progress(content: str, *, tool_hint: bool = False) -> None:
        notes.append(content)

    class _Runtime:
        async def run_status_changed(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _fake_run)
    monkeypatch.setattr("nanobot.agent.loop.turn_continuation.maybe_continue_turn", _continue)
    monkeypatch.setattr(AgentLoop, "_run_agent_loop", _normal)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = "none"
    loop.stepwise_enabled = True
    loop.workspace = None
    loop.provider = object()
    loop.model = "m"
    loop._runtime_events = lambda: _Runtime()
    ctx = SimpleNamespace(
        msg=SimpleNamespace(content="두 기록이 상충합니다. 상태를 확정해 주세요.", channel="telegram", chat_id="1", metadata={}),
        session_key="telegram:9", visible_run_started_at=None, initial_messages=[], on_progress=_progress,
        on_stream=None, on_stream_end=None, on_retry_wait=None, session=None, pending_queue=None,
        ephemeral=False, run_extra_hooks_for_ephemeral=False, hooks=[], tools=None, final_content=None,
        tools_used=None, all_messages=None, stop_reason=None, had_injections=None,
    )

    asyncio.run(loop._state_run(ctx))

    assert FALLBACK_NOTICE in notes
    assert ctx.final_content == "일반 답"

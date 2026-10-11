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
    assert case.status == "stopped"
    assert "실행하지 않았습니다" in case.stop_reason
    record = json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])
    assert any(item.startswith("review_unhandled") for item in record["defects"])
    assert [item["call_kind"] for item in record["calls"]] == ["compose", "review"] * 3


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

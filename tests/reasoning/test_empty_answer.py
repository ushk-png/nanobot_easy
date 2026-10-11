"""Empty final answers must not complete a stepwise run (design 4.4, 11).

Observed on 2026-10-11: a dynamic procedure of work steps followed by an
answer step finished with every step "passed" and an empty answer. Each step
call received the same payload (request plus empty case fields and the step
id), without the step's question, expected outputs or earlier step results,
and the model returned an empty ``answer``.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.reasoning.dynamic import validate_dynamic_procedure
from nanobot.reasoning.engine import ReasoningEngine

REQUEST = "후보 보고서 세 개 중 조건에 맞는 하나를 근거와 함께 골라 주세요."


def _selection_procedure():
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "조건에 맞는 보고서 하나를 근거와 함께 고른다.",
            "steps": [
                {"id": "check", "question": "후보별 조건 충족을 판정한다.", "inputs": ["request"], "outputs": ["checked"]},
                {"id": "filter", "question": "조건 미달 후보를 제외한다.", "inputs": ["check"], "outputs": ["remaining"]},
                {"id": "pick", "question": "남은 후보 중 하나를 고른다.", "inputs": ["filter"], "outputs": ["selection"]},
                {"id": "justify", "role": "answer", "question": "선정 결과와 근거를 답한다.", "inputs": ["pick"], "outputs": ["answer"]},
            ],
        },
        request=REQUEST,
    )
    assert defects == [] and procedure is not None
    return procedure


class EchoLLM:
    """Mimics the observed model: fills only what the payload asks for."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads[stage] = payload
        expected = payload.get("expected_outputs") or []
        result: dict[str, Any] = {"answer": ""}
        for key in expected:
            result[key] = "보고서 B를 고릅니다. 세 조건을 모두 충족합니다." if key == "answer" else f"{stage} 결과"
        return result


class BlankAnswerLLM(EchoLLM):
    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = super().complete(stage, payload)
        result["answer"] = "  "
        return result


@pytest.mark.xfail(strict=True, reason="stage 2 reproduction; fixed in stage 3")
def test_step_payload_carries_the_step_contract() -> None:
    llm = EchoLLM()
    ReasoningEngine(llm).run(REQUEST, procedure=_selection_procedure())

    payload = llm.payloads["justify"]
    assert payload["question"] == "선정 결과와 근거를 답한다."
    assert payload["expected_outputs"] == ["answer"]


@pytest.mark.xfail(strict=True, reason="stage 2 reproduction; fixed in stage 3")
def test_all_steps_passing_with_blank_answer_is_not_completed() -> None:
    case = ReasoningEngine(BlankAnswerLLM()).run(REQUEST, procedure=_selection_procedure())

    assert case.answer.strip() == ""
    assert case.status != "completed"
    assert case.status == "failed_empty_answer"
    assert case.step_states["justify"] != "passed"

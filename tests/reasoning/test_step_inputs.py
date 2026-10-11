"""Each step receives its declared inputs, not every earlier result (design 12)."""

from __future__ import annotations

import json
from typing import Any

from nanobot.reasoning.dynamic import validate_dynamic_procedure
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS

REQUEST = "후보 중 하나를 근거와 함께 골라 주세요."


class RecordingLLM:
    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads[stage] = payload
        return {
            key: ("B를 고릅니다." if key == "answer" else f"{stage}-{key}")
            for key in payload.get("expected_outputs") or []
        }


def _chain():
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "후보 하나를 근거와 함께 고른다.",
            "requirements": [{"id": "R1", "statement": "외부 통신 없음"}],
            "steps": [
                {"id": "collect", "question": "후보를 모은다.", "outputs": ["candidates"]},
                {"id": "check", "question": "R1 충족을 판정한다.", "inputs": ["collect", "R1"], "outputs": ["verdicts"]},
                {"id": "pick", "question": "하나를 선정한다.", "inputs": ["check"], "outputs": ["selection"]},
                {"id": "justify", "role": "answer", "question": "선정과 근거를 답한다.", "inputs": ["pick"], "outputs": ["answer"]},
            ],
        },
        request=REQUEST,
    )
    assert defects == [] and procedure is not None
    return procedure


def test_step_payload_contains_only_declared_inputs() -> None:
    llm = RecordingLLM()
    case = ReasoningEngine(llm).run(REQUEST, procedure=_chain())

    assert case.status == "completed"
    assert llm.payloads["pick"]["inputs"] == {"check": {"verdicts": "check-verdicts"}}
    assert llm.payloads["justify"]["inputs"] == {"pick": {"selection": "pick-selection"}}
    assert "collect" not in json.dumps(llm.payloads["justify"]["inputs"])
    assert llm.payloads["check"]["inputs"]["R1"] == "R1: 외부 통신 없음"
    for payload in llm.payloads.values():
        assert payload["request"] == REQUEST


def test_typed_steps_receive_only_the_fields_their_role_reads() -> None:
    llm = ScriptedStageLLM(STAGE_OUTPUTS)
    payloads: dict[str, dict[str, Any]] = {}
    original = llm.complete

    def complete(stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        payloads[stage] = payload
        return original(stage, payload)

    llm.complete = complete  # type: ignore[method-assign]
    case = ReasoningEngine(llm).run("일정 충돌 조정 방법 평가")

    assert case.status == "completed"
    assert "criteria" not in payloads["S2"] and "judgments" not in payloads["S2"]
    assert "facts" in payloads["S4"] and "criteria" in payloads["S4"]
    assert "facts" not in payloads["S7"]
    assert "conclusion" in payloads["S7"]
    assert len(json.dumps(payloads["S2"], ensure_ascii=False)) < len(
        json.dumps(payloads["S6"], ensure_ascii=False)
    )


def test_answer_step_without_step_inputs_is_rejected() -> None:
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "후보 하나를 근거와 함께 고른다.",
            "steps": [
                {"id": "check", "question": "후보를 판정한다.", "outputs": ["judgments"]},
                {"id": "justify", "role": "answer", "question": "답한다.", "inputs": ["request"], "outputs": ["answer"]},
            ],
        },
        request=REQUEST,
    )

    assert procedure is None
    assert any(item.startswith("answer_inputs_missing") for item in defects)


def test_malformed_typed_output_is_a_step_defect_not_a_crash() -> None:
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "후보 하나를 근거와 함께 고른다.",
            "steps": [
                {"id": "check", "question": "판정한다.", "outputs": ["judgments"]},
                {"id": "justify", "role": "answer", "question": "답한다.", "inputs": ["check"], "outputs": ["answer"]},
            ],
        },
        request=REQUEST,
    )
    assert defects == [] and procedure is not None

    case = ReasoningEngine(RecordingLLM()).run(REQUEST, procedure=procedure)

    assert case.status == "stopped"
    assert "judgments output has an invalid shape" in case.stop_reason
    assert "justify" not in [item.get("stage") for item in case.history if item.get("event") == "stage_result"]

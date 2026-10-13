"""Dynamic procedures may execute once but are not reusable registrations."""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.reasoning.bridge import run_reasoning_procedure
from nanobot.reasoning.dynamic import validate_dynamic_procedure
from nanobot.reasoning.registry import approved_procedures


class _ProposalLLM:
    def __init__(self, proposal: dict) -> None:
        self.proposal = proposal
        self.calls: list[str] = []

    def complete(self, stage: str, payload: dict) -> dict:
        del payload
        self.calls.append(stage)
        if stage == "procedure":
            return self.proposal
        if stage == "review":
            return {
                "request_items": [{"item": "겹치는 일정 조정", "step_id": "S7"}],
                "answer_step_id": "S7",
                "defects": [],
            }
        goal = "일정 충돌 조정"
        return {
            "S1": {"goal": goal, "constraints": [], "questions": []},
            "S2": {"facts": [{"id": "f1", "proposition": "두 일정이 겹침", "source": "request", "status": "confirmed"}]},
            "S3": {"criteria": [{"id": "c1", "statement": "충돌을 해소해야 한다.", "kind": "required", "method": "time", "linked_goal": goal}]},
            "S4": {"judgments": [{"id": "j1", "criterion_id": "c1", "result": "met", "evidence_ids": ["f1"], "step_id": "S4"}]},
            "S5": {"conclusion": {"id": "k1", "status": "suitable", "statement": "시간을 조정한다.", "conditions": [], "judgment_ids": ["j1"]}},
            "S6": {"defects": []},
            "S7": {"answer": "겹치는 일정의 시간을 조정합니다."},
        }.get(stage, {})


def _proposal() -> dict:
    return {
        "id": "schedule-conflict",
        "title": "일정 충돌 조정",
        "purpose": "겹치는 일정의 조정 순서를 판단한다.",
        "steps": [{"id": f"S{i}", "question": f"{i}단계 판단"} for i in range(1, 8)],
    }


def test_approved_s1_s7_shape_still_validates() -> None:
    procedure, defects = validate_dynamic_procedure(_proposal(), request="겹치는 일정 조정")

    assert defects == []
    assert procedure is not None
    assert [step.id for step in procedure.steps] == [f"S{i}" for i in range(1, 8)]


def test_valid_dynamic_procedure_is_not_registered() -> None:
    procedure, defects = validate_dynamic_procedure(_proposal(), request="겹치는 일정 조정")

    assert defects == []
    assert procedure is not None
    assert procedure.dynamic is True
    assert procedure.reusable is False
    assert procedure.id not in approved_procedures()


def test_variable_length_procedure_is_executable() -> None:
    payload = {
        "purpose": "일정 조정 순서를 판단한다.",
        "steps": [
            {"id": "D1", "role": "goal", "question": "조정 목표를 확인한다.", "outputs": ["goal"]},
            {"id": "D2", "role": "facts", "question": "겹치는 일정을 확인한다.", "inputs": ["D1"], "outputs": ["facts"]},
            {"id": "D3", "role": "answer", "question": "조정안을 답한다.", "inputs": ["D2"], "outputs": ["answer"]},
        ],
    }

    procedure, defects = validate_dynamic_procedure(payload, request="일정 조정")

    assert defects == []
    assert procedure is not None
    assert [step.id for step in procedure.steps] == ["D1", "D2", "D3"]
    assert procedure.id
    assert procedure.title


def test_missing_reference_is_not_executable() -> None:
    payload = _proposal()
    payload["steps"][1]["inputs"] = ["missing-step"]
    procedure, defects = validate_dynamic_procedure(payload, request="일정 조정")

    assert procedure is None
    assert "missing_reference" in defects[0]


def test_metadata_only_gap_needs_no_extra_generation() -> None:
    payload = {
        "purpose": "일정 조정 순서를 판단한다.",
        "steps": [
            {"question": "조정 목표를 확인한다.", "role": "goal", "outputs": ["goal"]},
            {"question": "조정안을 답한다.", "role": "answer", "inputs": ["D1"], "outputs": ["answer"]},
        ],
    }

    procedure, defects = validate_dynamic_procedure(payload, request="일정 조정")

    assert defects == []
    assert procedure is not None
    assert procedure.steps[0].id == "D1"
    assert procedure.steps[1].inputs == ["D1"]


def test_parse_failure_is_distinct_from_missing_metadata() -> None:
    from nanobot.reasoning.contract import parse_procedure_payload

    payload, defects = parse_procedure_payload('{"purpose":')

    assert payload is None
    assert defects[0].code == "parse_failed"


def test_known_goal_fills_missing_purpose_without_overwrite() -> None:
    payload = {
        "steps": [
            {"id": "D1", "role": "answer", "question": "일정 조정안을 답한다.", "outputs": ["answer"]},
        ]
    }

    procedure, defects = validate_dynamic_procedure(
        payload,
        request="일정 조정",
        known_goal="일정 조정 순서를 판단한다.",
    )

    assert defects == []
    assert procedure is not None
    assert procedure.purpose == "일정 조정 순서를 판단한다."

    conflict, conflict_defects = validate_dynamic_procedure(
        {**payload, "purpose": "다른 목적"},
        request="일정 조정",
        known_goal="일정 조정 순서를 판단한다.",
    )

    # Structural checking cannot decide semantic purpose equivalence. The separate
    # mandatory fitness review owns rejection of a genuinely different purpose.
    assert conflict is not None
    assert conflict.purpose == "다른 목적"
    assert conflict_defects == []


def test_missing_purpose_without_known_goal_is_not_invented() -> None:
    payload = {
        "steps": [
            {"id": "D1", "role": "answer", "question": "답한다.", "outputs": ["answer"]},
        ]
    }

    procedure, defects = validate_dynamic_procedure(payload, request="일정 조정")

    assert procedure is None
    assert "purpose_missing" in defects[0]


def test_known_legacy_shape_converts_and_unknown_version_does_not() -> None:
    legacy = {
        "schema_version": "3.0",
        "goal": "일정 조정 순서를 판단한다.",
        "stages": [
            {"id": "D1", "role": "answer", "question": "일정 조정안을 답한다.", "outputs": ["answer"]},
        ],
    }

    procedure, defects = validate_dynamic_procedure(legacy, request="일정 조정")

    assert defects == []
    assert procedure is not None
    assert procedure.version == "3.1"
    assert procedure.purpose == "일정 조정 순서를 판단한다."

    unknown, unknown_defects = validate_dynamic_procedure(
        {**legacy, "schema_version": "9.9"},
        request="일정 조정",
    )

    assert unknown is None
    assert "unknown_schema_version" in unknown_defects[0]


def test_duplicate_step_id_and_cycle_are_rejected() -> None:
    duplicate, duplicate_defects = validate_dynamic_procedure(
        {
            "purpose": "일정 조정 순서를 판단한다.",
            "steps": [
                {"id": "D1", "question": "목표를 확인한다.", "outputs": ["goal"]},
                {"id": "D1", "question": "답한다.", "outputs": ["answer"]},
            ],
        },
        request="일정 조정",
    )
    cycle, cycle_defects = validate_dynamic_procedure(
        {
            "purpose": "일정 조정 순서를 판단한다.",
            "steps": [
                {"id": "D1", "question": "목표를 확인한다.", "inputs": ["D2"], "outputs": ["goal"]},
                {"id": "D2", "role": "answer", "question": "답한다.", "inputs": ["D1"], "outputs": ["answer"]},
            ],
        },
        request="일정 조정",
    )

    assert duplicate is None
    assert "duplicate_step_id" in duplicate_defects[0]
    assert cycle is None
    assert "dependency_cycle" in cycle_defects[0]


REPORT_TOOL_REQUEST = (
    "내부망에서 사용할 보고서 분석 도구를 선정해 주세요. "
    "필수 조건은 외부 통신 없이 작동, 감사 로그 제공, 하루 1,000건 이상 처리, "
    "도입비 5,000만 원 이하입니다."
)


def _report_tool_attempts() -> list[dict]:
    """Preserve the observed requirements -> check -> choose -> justify shape.

    The original generated JSON was not retained. These fixtures keep the
    confirmed step ids and the reported failure mode without copying request
    fragments into step text.
    """

    return [
        {
            "purpose": "보고서 분석 도구를 선정한다.",
            "steps": [
                {"id": "requirements", "question": "조건을 정리한다.", "outputs": ["requirements"]},
                {"id": "check", "question": "제품별 충족 여부를 판정한다.", "inputs": ["requirements"], "outputs": ["judgments"]},
                {"id": "choose", "question": "제품을 선정한다.", "inputs": ["check"], "outputs": ["selection"]},
                {"id": "justify", "role": "answer", "question": "선정 이유를 답한다.", "inputs": ["choose"], "outputs": ["answer"]},
            ],
        },
        {
            "purpose": "보고서 분석 도구를 선정한다.",
            "requirements": ["R1"],
            "steps": [
                {"id": "requirements", "question": "R1을 정리한다.", "outputs": ["requirements"]},
                {"id": "check", "question": "R1을 판정한다.", "inputs": ["requirements"], "outputs": ["judgments"]},
                {"id": "choose", "question": "제품을 선정한다.", "inputs": ["check"], "outputs": ["selection"]},
                {"id": "justify", "role": "answer", "question": "선정 이유를 답한다.", "inputs": ["choose"], "outputs": ["answer"]},
            ],
        },
        {
            "purpose": "보고서 분석 도구를 선정한다.",
            "requirements": [{"id": "R1", "statement": "외부 통신 없이 작동"}],
            "steps": [
                {"id": "requirements", "question": "요구사항을 정리한다.", "outputs": ["requirements"]},
                {"id": "check", "question": "제품별 충족, 불충족, 미확인을 판정한다.", "inputs": ["R1"], "outputs": ["judgments"]},
                {"id": "choose", "question": "판정 결과로 제품을 선정한다.", "inputs": ["check"], "outputs": ["selection"]},
                {"id": "justify", "role": "answer", "question": "선정 이유를 답한다.", "inputs": ["choose"], "outputs": ["answer"]},
            ],
        },
    ]


def test_failed_report_tool_attempts_are_not_rejected_for_wording() -> None:
    first, second, third = _report_tool_attempts()

    executable, first_defects = validate_dynamic_procedure(first, request=REPORT_TOOL_REQUEST)
    identifier_only, second_defects = validate_dynamic_procedure(second, request=REPORT_TOOL_REQUEST)
    linked, third_defects = validate_dynamic_procedure(third, request=REPORT_TOOL_REQUEST)

    assert executable is not None and first_defects == []
    assert identifier_only is None
    assert "requirement_unlinked" in second_defects[0]
    assert "requirement_missing" not in second_defects[0]
    assert linked is not None and third_defects == []


def test_request_wording_is_not_required_in_step_text() -> None:
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "보고서 분석 도구를 선정한다.",
            "steps": [
                {"id": "requirements", "question": "조건을 정리한다.", "outputs": ["requirements"]},
                {"id": "check", "question": "조건을 판정한다.", "inputs": ["requirements"], "outputs": ["judgments"]},
                {"id": "choose", "question": "제품을 선정한다.", "inputs": ["check"], "outputs": ["selection"]},
                {"id": "justify", "role": "answer", "question": "이유를 답한다.", "inputs": ["choose"], "outputs": ["answer"]},
            ],
        },
        request=REPORT_TOOL_REQUEST,
    )

    assert defects == []
    assert procedure is not None


def test_requirement_identifier_alone_is_not_coverage() -> None:
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "보고서 분석 도구를 선정한다.",
            "requirements": [{"id": "R1", "statement": "외부 통신 없이 작동"}],
            "steps": [
                {"id": "requirements", "question": "R1을 적는다.", "outputs": ["requirements"]},
                {"id": "choose", "question": "R1 제품을 선정한다.", "inputs": ["requirements"], "outputs": ["selection"]},
                {"id": "justify", "role": "answer", "question": "이유를 답한다.", "inputs": ["choose"], "outputs": ["answer"]},
            ],
        },
        request=REPORT_TOOL_REQUEST,
    )

    assert procedure is None
    assert "requirement_unlinked" in defects[0]


def test_requirement_reaches_answer_without_forcing_selection() -> None:
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "보고서 분석 도구를 선정한다.",
            "requirements": [{"id": "R1", "statement": "외부 통신 없이 작동"}],
            "steps": [
                {"id": "check", "question": "R1의 충족 여부를 판정한다.", "inputs": ["R1"], "outputs": ["judgments"]},
                {"id": "justify", "role": "answer", "question": "판정만 보고한다.", "inputs": ["check"], "outputs": ["answer"]},
            ],
        },
        request=REPORT_TOOL_REQUEST,
    )

    # Requirement linkage is valid. Whether reporting alone fulfills this request
    # is a fitness-review question, not a reason to force selection on every task.
    assert procedure is not None
    assert defects == []


def test_conflicting_wrappers_are_not_silently_selected() -> None:
    payload = {
        "procedure": {"purpose": "일정 조정", "steps": [{"id": "D1", "question": "답한다", "outputs": ["answer"]}]},
        "plan": {"purpose": "다른 계획", "steps": [{"id": "P1", "question": "다른 답", "outputs": ["answer"]}]},
    }

    procedure, defects = validate_dynamic_procedure(payload, request="일정 조정")

    assert procedure is None
    assert "ambiguous_object" in defects[0]


def test_dynamic_execution_is_recorded_without_reuse(monkeypatch, tmp_path: Path) -> None:
    import nanobot.reasoning.bridge as bridge

    monkeypatch.setattr(bridge, "ProviderStageLLM", lambda *_args: _ProposalLLM(_proposal()))
    import asyncio

    case = asyncio.run(
        run_reasoning_procedure(
            provider=object(),
            model="test",
            request="겹치는 일정 조정",
            workspace=tmp_path,
        )
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()
    ]

    assert case.status == "completed"
    assert records[0]["reusable"] is False
    assert records[0]["registration"] == "not_evaluated"
    assert records[0]["answer"] == "겹치는 일정의 시간을 조정합니다."
    assert records[0]["step_states"]["S7"] == "passed"
    assert "schedule-conflict" not in approved_procedures()


def test_empty_completed_answer_is_not_reported_as_internal_status(monkeypatch, tmp_path: Path) -> None:
    import nanobot.reasoning.bridge as bridge

    class _EmptyAnswer(_ProposalLLM):
        def complete(self, stage: str, payload: dict) -> dict:
            result = super().complete(stage, payload)
            if stage == "S7":
                result["answer"] = "   "
            return result

    monkeypatch.setattr(bridge, "ProviderStageLLM", lambda *_args: _EmptyAnswer(_proposal()))
    import asyncio

    notes: list[str] = []

    async def _progress(content: str, *, tool_hint: bool = False) -> None:
        del tool_hint
        notes.append(content)

    case = asyncio.run(
        run_reasoning_procedure(
            provider=object(),
            model="test",
            request="겹치는 일정 조정",
            workspace=tmp_path,
            on_progress=_progress,
        )
    )

    assert case.status == "failed_empty_answer"
    assert case.answer.strip() == ""
    assert "procedure completed" not in case.stop_reason
    assert "최종 답변 단계(S7)" in case.stop_reason
    assert "완료로 처리하지 않았습니다" in case.stop_reason
    assert notes[0] == "절차를 구성하고 있습니다."
    assert any(item.endswith("단계를 마쳤습니다.") for item in notes)

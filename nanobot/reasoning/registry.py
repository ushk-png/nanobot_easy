"""Approved reusable reasoning procedures.

A dynamic procedure may execute once after the shared contract passes.
That execution does not add it to this registry.
"""

from __future__ import annotations

from nanobot.reasoning.models import Procedure, ProcedureStep

PROCEDURE_VERSION = "3.1"

_PROCEDURES: dict[str, tuple[str, tuple[str, ...]]] = {
    "method-evaluation": (
        "방법 평가",
        (
            "달성 목표, 평가 대상, 필수 제약을 구분한다.",
            "사실, 추정, 미확인, 충돌을 구분한다.",
            "목적을 판정 가능한 필수·선호 기준으로 구체화한다.",
            "각 기준의 충족 여부와 근거를 판단한다.",
            "필수 조건과 미확인을 반영해 결론을 도출한다.",
            "결론과 의존 근거의 구조·규칙을 검사한다.",
            "통과한 결론과 조건을 사용자 답변으로 작성한다.",
        ),
    ),
    "cause-analysis": (
        "원인분석",
        (
            "문제와 확인해야 할 원인을 정의한다.",
            "사실과 시간순서를 구분하고 미확인을 표시한다.",
            "가능한 원인과 반증 기준을 정리한다.",
            "각 원인 후보의 관측 근거와 대안 설명을 판단한다.",
            "근거가 연결되는 원인과 조치 조건을 결론으로 정리한다.",
            "시간순서·대안 설명·근거 연결을 검사한다.",
            "확정하지 못한 원인과 조건을 포함해 답변한다.",
        ),
    ),
    "problem-solving": (
        "문제 해결",
        (
            "현재 상태, 목표 상태, 제약을 구분한다.",
            "차이와 확인된 사실, 미확인 정보를 정리한다.",
            "해결 방안의 필수 조건과 선호 조건을 정한다.",
            "각 방안이 조건과 제약을 충족하는지 판단한다.",
            "실행 가능한 제안과 적용 조건을 결론으로 정리한다.",
            "제약 위반과 조건 누락을 검사한다.",
            "제안과 실행 조건을 답변으로 작성한다.",
        ),
    ),
    "document-review": (
        "문서 검토",
        (
            "문서의 작성 목적과 검토 대상을 확인한다.",
            "주장, 근거, 누락, 모순 후보를 사실로 구분한다.",
            "검토 목적에 맞는 필수·선호 기준을 정한다.",
            "주장별 근거 충분성과 수정 필요성을 판단한다.",
            "수정 방향과 유지할 결론을 정리한다.",
            "근거 없는 수정과 목적 불일치를 검사한다.",
            "수정안과 남은 불확실성을 답변으로 작성한다.",
        ),
    ),
    "rule-judgment": (
        "규칙 판단",
        (
            "적용할 규정과 판단 대상을 확인한다.",
            "규정, 사실, 예외, 누락을 구분한다.",
            "규정에서 판정 가능한 필수 조건을 추출한다.",
            "각 조건의 충족 여부와 근거를 판단한다.",
            "코드와 Z3 판정 범위를 반영해 결론을 정리한다.",
            "전제 일관성과 결론의 논리적 귀결을 검사한다.",
            "적용 조건과 검증 범위를 포함해 답변한다.",
        ),
    ),
}


def procedure(procedure_id: str, granularity: str = "fine") -> Procedure:
    """Return one reviewed procedure. Unknown IDs are rejected.

    ``granularity="compact"`` merges logical steps into fewer calls for models
    that reason internally (design 10); every step's code checks still run.
    """

    try:
        title, questions = _PROCEDURES[procedure_id]
    except KeyError as exc:
        raise KeyError(f"unapproved procedure: {procedure_id}") from exc
    if granularity == "compact":
        return _compact(procedure_id, title, questions)
    roles = ("goal", "facts", "criteria", "judgments", "conclusion", "check", "answer")
    outputs = ("goal", "facts", "criteria", "judgments", "conclusion", "checks", "answer")
    returns = (None, "S1", "S1", "S2", "S4", "S5", "S5")
    return Procedure(
        id=procedure_id,
        version=PROCEDURE_VERSION,
        title=title,
        purpose=title,
        steps=[
            ProcedureStep(
                id=f"S{index}",
                role=roles[index - 1],
                question=question,
                inputs=["request"] if index == 1 else [f"S{index - 1}"],
                outputs=[outputs[index - 1]],
                checks=["structure"] if index < 7 else ["faithfulness"],
                failure_returns_to=returns[index - 1],
                version=PROCEDURE_VERSION,
            )
            for index, question in enumerate(questions, start=1)
        ],
    )


def _compact(procedure_id: str, title: str, questions: tuple[str, ...]) -> Procedure:
    q = questions
    layout = (
        ("S1-2", "goal", ["facts"], f"{q[0]} {q[1]}", ["request"], ["goal", "facts"], None),
        (
            "S3-5",
            "criteria",
            ["judgments", "conclusion"],
            f"{q[2]} {q[3]} {q[4]}",
            ["S1-2"],
            ["criteria", "judgments", "conclusion"],
            "S1-2",
        ),
        ("S6", "check", [], q[5], ["S3-5"], ["checks"], "S3-5"),
        ("S7", "answer", [], q[6], ["S6"], ["answer"], "S3-5"),
    )
    return Procedure(
        id=procedure_id,
        version=PROCEDURE_VERSION,
        title=title,
        purpose=title,
        granularity="compact",
        steps=[
            ProcedureStep(
                id=step_id,
                role=role,
                merged_roles=merged,
                question=question,
                inputs=inputs,
                outputs=outputs,
                checks=["structure"] if role != "answer" else ["faithfulness"],
                failure_returns_to=returns,
                version=PROCEDURE_VERSION,
            )
            for step_id, role, merged, question, inputs, outputs, returns in layout
        ],
    )


def method_evaluation() -> Procedure:
    return procedure("method-evaluation")


def approved_procedures() -> tuple[str, ...]:
    return tuple(_PROCEDURES)

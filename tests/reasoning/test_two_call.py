"""Two-call layout: A (goal+facts+criteria), B (judgments+conclusion+answer), code between."""

from __future__ import annotations

import copy
from typing import Any

from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from nanobot.reasoning.registry import procedure

GOAL = "보고서 검토 자동화 방법의 적합성 판단"

A_OUT = {
    "goal": GOAL,
    "constraints": ["내부망", "근거 제시"],
    "questions": [],
    "facts": [
        {"id": "f1", "proposition": "제안 방법은 외부 API를 쓴다", "source": "request", "status": "confirmed"},
        {"id": "f2", "proposition": "근거 문장 표시 가능 여부", "source": "request", "status": "unknown"},
        {"id": "f3", "proposition": "처리 속도가 빠르다", "source": "request", "status": "confirmed"},
    ],
    "criteria": [
        {"id": "c1", "statement": "내부망에서 실행", "kind": "required", "method": "구성 확인", "linked_goal": GOAL},
        {"id": "c2", "statement": "근거 문장 제시", "kind": "required", "method": "기능 확인", "linked_goal": GOAL},
        {"id": "c3", "statement": "처리 속도", "kind": "preferred", "method": "비교", "linked_goal": GOAL},
    ],
}

B_OUT = {
    "judgments": [
        {"id": "j1", "criterion_id": "c1", "result": "unmet", "evidence_ids": ["f1"]},
        {"id": "j2", "criterion_id": "c2", "result": "unknown", "evidence_ids": ["f2"]},
        {"id": "j3", "criterion_id": "c3", "result": "met", "evidence_ids": ["f3"]},
    ],
    "conclusion": {
        "id": "k1",
        "status": "unsuitable",
        "statement": "외부 API 사용으로 내부망 조건 불충족",
        "conditions": [],
        "judgment_ids": ["j1", "j2"],
    },
    "answer": "외부 API를 쓰므로 내부망 조건을 충족하지 못해 부적합합니다. 근거 문장 표시는 아직 확인되지 않았습니다.",
    "answer_claims": {"status": "unsuitable", "covers": ["c1"], "unknown_disclosed": ["c2"], "conditions_included": []},
    "risks": [],
}


class Script(ScriptedStageLLM):
    """Per-stage queues; the last entry repeats."""

    def __init__(self, queues: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__({})
        self.queues = {key: [copy.deepcopy(item) for item in value] for key, value in queues.items()}
        self.payloads: list[tuple[str, dict[str, Any]]] = []

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(stage)
        self.payloads.append((stage, payload))
        queue = self.queues[stage]
        return copy.deepcopy(queue.pop(0) if len(queue) > 1 else queue[0])


def _b(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(B_OUT)
    for key, value in changes.items():
        out[key] = value
    return out


def _run(llm: Script, **engine_kwargs: Any):
    return ReasoningEngine(llm, **engine_kwargs).run("평가해 주세요", procedure=procedure("method-evaluation", "two_call"))


def test_layout_keeps_roles_but_uses_two_calls() -> None:
    two = procedure("method-evaluation", "two_call")

    assert [step.id for step in two.steps] == ["A", "B", "S6"]
    assert two.step("A").roles == ["goal", "facts", "criteria"]
    assert two.step("B").roles == ["judgments", "conclusion", "answer"]
    assert two.step("S6").op == "review"
    assert two.conclusion_rule == "required_criteria"


def test_default_path_is_two_model_calls_with_review_skipped() -> None:
    llm = Script({"A": [A_OUT], "B": [B_OUT]})
    case = _run(llm)

    assert case.status == "completed"
    assert llm.calls == ["A", "B"]
    checks = {item.kind: item for item in case.checks}
    assert checks["llm:S6"].result == "SKIPPED"
    assert "code chain checks still ran" in checks["llm:S6"].limit
    assert checks["code:S6"].result == "pass"
    assert checks["code:conclusion_rule"].result == "pass"
    assert "wording itself is not verified" in checks["code:answer_claims"].limit
    decision = next(item for item in case.history if item.get("event") == "review_decision")
    assert decision["run"] is False  # an unsuitable verdict with an unknown is not a trigger


def test_b_receives_the_conclusion_rule_and_structured_claim_shape() -> None:
    llm = Script({"A": [A_OUT], "B": [B_OUT]})
    _run(llm)

    b_payload = next(payload for stage, payload in llm.payloads if stage == "B")
    assert "unmet이면 unsuitable" in b_payload["conclusion_rule"]
    assert {"answer_claims", "risks"} <= set(b_payload["output_shape"])
    assert "exception_rule" in b_payload["output_shape"]["conclusion"]
    assert "facts" in b_payload and "criteria" in b_payload


def test_conclusion_against_the_rule_re_requests_only_conclusion_and_answer() -> None:
    wrong = _b(
        conclusion={**B_OUT["conclusion"], "status": "conditionally_suitable", "conditions": ["확인 필요"]},
        answer_claims={**B_OUT["answer_claims"], "status": "conditionally_suitable", "conditions_included": [0]},
    )
    fixed = _b(judgments=[{"id": "jX", "criterion_id": "c1", "result": "met", "evidence_ids": ["f1"]}])
    llm = Script({"A": [A_OUT], "B": [wrong, fixed]})
    case = _run(llm)

    assert case.status == "completed"
    assert llm.calls == ["A", "B", "B"]
    retry_payload = llm.payloads[-1][1]
    assert retry_payload["expected_outputs"] == ["conclusion", "answer", "answer_claims", "risks"]
    assert [item["id"] for item in retry_payload["fixed"]["judgments"]] == ["j1", "j2", "j3"]
    assert any(item.startswith("conclusion_rule") for item in retry_payload["repair_defects"])
    assert [item.id for item in case.judgments] == ["j1", "j2", "j3"]  # judgments kept
    assert case.conclusion.status == "unsuitable"
    extra = [item for item in case.history if item.get("event") == "extra_call"]
    assert extra and extra[0]["reason"] == "defects in this step's own output"


def test_answer_stating_another_status_is_re_requested() -> None:
    mismatch = _b(answer_claims={**B_OUT["answer_claims"], "status": "suitable"})
    llm = Script({"A": [A_OUT], "B": [mismatch, B_OUT]})
    case = _run(llm)

    assert case.status == "completed"
    assert llm.calls == ["A", "B", "B"]
    assert any("answer states suitable" in item for item in llm.payloads[-1][1]["repair_defects"])


def test_template_fixable_gaps_are_fixed_without_a_call() -> None:
    undisclosed = _b(
        answer="외부 API를 쓰므로 내부망 조건을 충족하지 못해 부적합합니다.",
        answer_claims={**B_OUT["answer_claims"], "unknown_disclosed": []},
    )
    llm = Script({"A": [A_OUT], "B": [undisclosed]})
    case = _run(llm)

    assert llm.calls == ["A", "B"]
    assert case.answer.endswith("다음 필수 조건은 아직 확인되지 않았습니다: 근거 문장 제시.")
    assert any(item.get("event") == "template_fix" for item in case.history)


def test_flagged_risk_on_a_required_criterion_triggers_a_focused_review() -> None:
    risky = _b(risks=[{"judgment_id": "j1", "issue": "'외부 API'가 사내 게이트웨이를 포함하는지에 따라 달라짐", "question": "사내 API인가?"}])
    llm = Script({"A": [A_OUT], "B": [risky], "S6": [{"defects": []}]})
    case = _run(llm)

    assert llm.calls == ["A", "B", "S6"]
    review = llm.payloads[-1][1]
    assert set(review) >= {"review_items", "question", "expected_outputs"}
    assert "request" not in review and "facts" not in review and "conversation" not in review
    item = review["review_items"][0]
    assert item["judgment"]["id"] == "j1"
    assert [fact["id"] for fact in item["evidence"]] == ["f1"]
    assert case.status == "completed"
    assert next(item for item in case.checks if item.kind == "llm:S6").result == "pass"


def test_risk_on_a_preferred_criterion_does_not_trigger_review() -> None:
    llm = Script({"A": [A_OUT], "B": [_b(risks=[{"judgment_id": "j3", "issue": "속도 기준 해석"}])]})
    case = _run(llm)

    assert llm.calls == ["A", "B"]
    decision = next(item for item in case.history if item.get("event") == "review_decision")
    assert decision["ignored_risks"] == 1


def test_estimated_only_evidence_triggers_review_from_code() -> None:
    a_out = copy.deepcopy(A_OUT)
    a_out["facts"][0]["status"] = "estimated"
    llm = Script({"A": [a_out], "B": [B_OUT], "S6": [{"defects": []}]})
    case = _run(llm)

    assert llm.calls == ["A", "B", "S6"]
    decision = next(item for item in case.history if item.get("event") == "review_decision")
    assert decision["reasons"] == ["evidence_conflict"]


def test_claimed_exception_rule_goes_to_review_not_to_rule_rejection() -> None:
    exception = _b(
        conclusion={**B_OUT["conclusion"], "status": "conditionally_suitable", "conditions": ["게이트웨이 경유"],
                    "exception_rule": "규정상 사내 게이트웨이 경유는 내부망으로 본다"},
        answer_claims={**B_OUT["answer_claims"], "status": "conditionally_suitable", "conditions_included": [0]},
    )
    llm = Script({"A": [A_OUT], "B": [exception], "S6": [{"defects": []}]})
    case = _run(llm)

    assert llm.calls == ["A", "B", "S6"]
    decision = next(item for item in case.history if item.get("event") == "review_decision")
    assert "new_rule" in decision["reasons"]


def test_required_policy_always_reviews() -> None:
    two = procedure("method-evaluation", "two_call")
    two.review_policy = "required"
    llm = Script({"A": [A_OUT], "B": [B_OUT], "S6": [{"defects": []}]})
    ReasoningEngine(llm).run("평가", procedure=two)

    assert llm.calls == ["A", "B", "S6"]


def test_unresolved_review_findings_leave_the_verdict_unconfirmed() -> None:
    risky = _b(risks=[{"judgment_id": "j1", "issue": "해석 차이"}])
    finding = {"defects": [{"step": "B", "judgment_id": "j1", "issue": "f1은 사내 API일 수 있어 불충족 근거가 약하다"}]}
    llm = Script({"A": [A_OUT], "B": [risky], "S6": [finding]})
    case = _run(llm)

    assert llm.calls == ["A", "B", "S6", "B", "S6"]
    returned_b = llm.payloads[3][1]
    assert any("사내 API" in item for item in returned_b["repair_defects"])
    assert case.status == "completed"
    assert case.step_states["S6"] == "conditionally_passed"
    assert case.conclusion.status == "deferred"
    assert case.answer.startswith("점검에서 해결되지 않은 문제가 있어 결론을 확정하지 않았습니다(미확정)")


def test_fact_update_rejudges_only_affected_criteria_in_one_call() -> None:
    engine = ReasoningEngine(Script({"A": [A_OUT], "B": [B_OUT]}))
    two = procedure("method-evaluation", "two_call")
    case = engine.run("평가", procedure=two)
    updated_b = _b(
        judgments=[
            {"id": "j9", "criterion_id": "c1", "result": "met", "evidence_ids": ["f1"]},
            {"id": "j8", "criterion_id": "c2", "result": "met", "evidence_ids": ["f1"]},
        ],
        conclusion={**B_OUT["conclusion"], "status": "conditionally_suitable", "conditions": ["근거 문장 확인"],
                    "judgment_ids": ["j9", "j2"]},
        answer="로컬 실행이 가능해 내부망 조건은 충족합니다. 근거 문장 표시는 확인되지 않아 조건부 적합입니다.",
        answer_claims={"status": "conditionally_suitable", "covers": ["c1"], "unknown_disclosed": ["c2"],
                       "conditions_included": [0]},
    )
    engine.llm = Script({"B": [updated_b]})

    engine.continue_with_facts(
        case, two, [{"id": "f1", "proposition": "로컬 모델로 실행 가능", "source": "user", "status": "confirmed"}]
    )

    assert engine.llm.calls == ["B"]
    b_payload = engine.llm.payloads[0][1]
    assert [item["id"] for item in b_payload["kept_judgments"]] == ["j2", "j3"]
    assert b_payload["rejudge_criteria"] == ["c1"]
    ids = [item.id for item in case.judgments]
    assert ids == ["j2", "j3", "j9"]  # j8 for the kept criterion c2 is ignored
    assert case.conclusion.status == "conditionally_suitable"
    assert case.status == "completed"


@__import__("pytest").mark.auto_granularity
def test_routing_call_prefills_step_a_so_the_default_path_is_two_calls(tmp_path) -> None:
    import asyncio
    import json
    from types import SimpleNamespace

    from nanobot.reasoning.bridge import _PROCEDURE_PROMPT, run_reasoning_procedure

    class Provider:
        def __init__(self, first_step: dict[str, Any]) -> None:
            self.first_step = first_step
            self.kinds: list[str] = []
            self.compose_body: dict[str, Any] = {}

        async def chat_with_retry(self, **kwargs):
            system = kwargs["messages"][0]["content"]
            body = json.loads(kwargs["messages"][1]["content"])
            if system == _PROCEDURE_PROMPT:
                self.kinds.append("compose")
                self.compose_body = body
                content = {"mode": "reuse", "procedure_id": "method-evaluation", "first_step_output": self.first_step}
            else:
                self.kinds.append(body["stage"])
                content = {"A": A_OUT, "B": B_OUT}[body["stage"]]
            return SimpleNamespace(content=json.dumps(content, ensure_ascii=False), usage={}, finish_reason="stop")

    good = Provider(A_OUT)
    case = asyncio.run(run_reasoning_procedure(good, model="m", request="평가해 줘", workspace=tmp_path))

    assert case.status == "completed"
    assert good.kinds == ["compose", "B"]
    catalog = {item["id"]: item for item in good.compose_body["approved_procedures"]}
    assert catalog["method-evaluation"]["first_step"]["id"] == "A"
    assert any(item.get("event") == "prefilled" for item in case.history)
    assert [item["call_kind"] for item in case.call_records] == ["compose", "answer"]

    # A prefill that fails A's code checks is not trusted: A is called with the defects.
    broken = {**A_OUT, "criteria": [{**A_OUT["criteria"][0], "kind": "preferred"}]}
    bad = Provider(broken)
    case = asyncio.run(run_reasoning_procedure(bad, model="m", request="평가해 줘", workspace=tmp_path))

    assert bad.kinds == ["compose", "A", "B"]
    assert case.status == "completed"


def test_risk_that_cannot_change_the_verdict_is_ignored_by_code() -> None:
    """B's flag is not the only basis: code checks the verdict would actually change."""

    a_out = copy.deepcopy(A_OUT)
    a_out["criteria"].append(
        {"id": "c4", "statement": "감사 로그", "kind": "required", "method": "기능 확인", "linked_goal": GOAL}
    )
    b_out = _b(
        judgments=[*B_OUT["judgments"], {"id": "j4", "criterion_id": "c4", "result": "unmet", "evidence_ids": ["f1"]}],
        conclusion={**B_OUT["conclusion"], "judgment_ids": ["j1", "j2", "j4"]},
        answer_claims={**B_OUT["answer_claims"], "covers": ["c1", "c4"]},
        risks=[{"judgment_id": "j1", "issue": "'외부 API' 해석"}],
    )
    llm = Script({"A": [a_out], "B": [b_out]})
    case = _run(llm)

    assert llm.calls == ["A", "B"]  # c4 stays unmet, so j1 cannot change 'unsuitable'
    decision = next(item for item in case.history if item.get("event") == "review_decision")
    assert decision["run"] is False and decision["ignored_risks"] == 1

"""Tests for the first code-controlled method-evaluation procedure."""

from __future__ import annotations

from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from nanobot.reasoning.z3check import check_entailment


def _scripts() -> dict[str, dict]:
    return {
        "S1": {
            "goal": "내부망에서 근거를 보여주는 보고서 검토",
            "constraints": ["내부망", "근거 문장 제시"],
            "questions": [],
            "usage": {"input_tokens": 20, "output_tokens": 10},
        },
        "S2": {
            "facts": [
                {
                    "id": "f1",
                    "proposition": "제안서는 외부 API를 사용한다.",
                    "source": "proposal:api",
                    "status": "confirmed",
                },
                {
                    "id": "f2",
                    "proposition": "내부망 대체 기능은 확인되지 않았다.",
                    "source": "proposal:deployment",
                    "status": "unknown",
                },
            ]
        },
        "S3": {
            "criteria": [
                {
                    "id": "c1",
                    "statement": "내부망에서 실행 가능해야 한다.",
                    "kind": "required",
                    "method": "deployment fact",
                    "linked_goal": "내부망에서 근거를 보여주는 보고서 검토",
                },
                {
                    "id": "c2",
                    "statement": "근거 문장을 추적할 수 있으면 좋다.",
                    "kind": "preferred",
                    "method": "evidence display",
                    "linked_goal": "내부망에서 근거를 보여주는 보고서 검토",
                },
            ]
        },
        "S4": {
            "judgments": [
                {
                    "id": "j1",
                    "criterion_id": "c1",
                    "result": "unmet",
                    "evidence_ids": ["f1"],
                    "step_id": "S4",
                },
                {
                    "id": "j2",
                    "criterion_id": "c2",
                    "result": "unknown",
                    "evidence_ids": ["f2"],
                    "step_id": "S4",
                },
            ]
        },
        "S5": {
            "conclusion": {
                "id": "k1",
                "status": "unsuitable",
                "statement": "현재 구성은 내부망 필수 조건을 충족하지 않는다.",
                "conditions": ["내부 실행 구조 확인 필요"],
                "judgment_ids": ["j1", "j2"],
            }
        },
        "S6": {},
        "S7": {
            "answer": "현재 구성은 내부망 조건을 충족하지 않습니다. 내부 실행 구조 확인 필요."
        },
    }


def test_method_evaluation_completes_without_promoting_unknowns() -> None:
    llm = ScriptedStageLLM(_scripts())
    case = ReasoningEngine(llm, reasoning_effort="none").run("이 방법이 적합한가요?")

    assert case.status == "completed"
    assert case.usage.reasoning_effort == "none"
    assert case.usage.reasoning_disabled_guaranteed is False
    assert [item.result for item in case.checks if item.kind.startswith("code:")].count("pass") == 7
    assert case.conclusion is not None
    assert case.conclusion.status == "unsuitable"
    assert "내부 실행 구조 확인 필요" in case.answer
    assert llm.calls == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]


def test_required_failure_cannot_be_marked_suitable() -> None:
    scripts = _scripts()
    scripts["S5"]["conclusion"]["status"] = "suitable"
    case = ReasoningEngine(ScriptedStageLLM(scripts), max_revisions=0).run("평가")

    assert case.status == "stopped"
    assert "required failure cannot be offset" in case.stop_reason
    assert case.step_states["S5"] == "stopped"


def test_unknown_evidence_cannot_satisfy_a_criterion() -> None:
    scripts = _scripts()
    scripts["S4"]["judgments"][0]["result"] = "met"
    scripts["S4"]["judgments"][0]["evidence_ids"] = ["f2"]
    case = ReasoningEngine(ScriptedStageLLM(scripts), max_revisions=0).run("평가")

    assert case.status == "stopped"
    assert "promotes unknown evidence" in case.stop_reason


def test_fact_update_invalidates_only_dependent_results() -> None:
    case = ReasoningEngine(ScriptedStageLLM(_scripts())).run("평가")
    ReasoningEngine(ScriptedStageLLM(_scripts())).update_facts(
        case,
        [
            {
                "id": "f1",
                "proposition": "제안서는 로컬 모델을 지원한다.",
                "source": "user:followup",
                "status": "confirmed",
            }
        ],
    )

    assert case.facts[0].version == 2
    assert [item.id for item in case.judgments] == ["j2"]
    assert case.conclusion is None
    assert case.answer == ""
    assert case.step_states["S4"] == "invalidated"
    assert case.step_states["S1"] == "passed"
    assert case.current_step == "S4"


def test_missing_goal_stops_before_later_stages() -> None:
    scripts = _scripts()
    scripts["S1"]["goal"] = ""
    llm = ScriptedStageLLM(scripts)
    case = ReasoningEngine(llm, max_revisions=0).run("평가")

    assert case.status == "stopped"
    assert llm.calls == ["S1"]


def test_ambiguous_request_waits_for_information() -> None:
    scripts = _scripts()
    scripts["S1"]["questions"] = ["검토할 방법 설명이 필요합니다."]
    case = ReasoningEngine(ScriptedStageLLM(scripts)).run("적합한가요?")

    assert case.status == "waiting_info"
    assert case.current_step == "S1"
    assert case.step_states["S2"] == "pending"


def test_z3_solver_reports_timeout_as_unknown(monkeypatch) -> None:
    import z3

    from nanobot.reasoning import z3check

    class _UnknownSolver:
        def __init__(self) -> None:
            self.assertions = []

        def set(self, **_kwargs) -> None:
            return None

        def add(self, value) -> None:
            self.assertions.append(value)

        def check(self):
            return z3.unknown

        def push(self) -> None:
            return None

        def pop(self) -> None:
            return None

    monkeypatch.setattr(z3check.z3, "Solver", _UnknownSolver)
    assert z3check.check_entailment({"internal_only": True}, ("internal_only", True)).value == "SOLVER_UNKNOWN"


def test_small_rule_entailment_states() -> None:
    assert check_entailment({"internal_only": True}, ("internal_only", True)).value == "ENTAILED"
    assert check_entailment({"internal_only": False}, ("internal_only", True)).value == "REFUTED"
    assert check_entailment({"internal_only": True}, ("evidence_visible", True)).value == "UNDETERMINED"
    assert check_entailment({"!internal_only": True, "internal_only": True}, ("internal_only", True)).value == "INCONSISTENT"

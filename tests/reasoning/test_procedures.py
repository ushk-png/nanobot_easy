"""Coverage for every approved v3 procedure and rejection of invented ones."""

from __future__ import annotations

import pytest

from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from nanobot.reasoning.registry import approved_procedures, procedure


def _scripts(goal: str) -> dict[str, dict]:
    return {
        "S1": {"goal": goal, "constraints": ["확인된 범위만"], "questions": []},
        "S2": {
            "facts": [
                {"id": "f1", "proposition": "관측된 사실", "source": "request", "status": "confirmed"}
            ]
        },
        "S3": {
            "criteria": [
                {
                    "id": "c1",
                    "statement": "관측 근거가 있어야 한다.",
                    "kind": "required",
                    "method": "source check",
                    "linked_goal": goal,
                }
            ]
        },
        "S4": {
            "judgments": [
                {"id": "j1", "criterion_id": "c1", "result": "met", "evidence_ids": ["f1"], "step_id": "S4"}
            ]
        },
        "S5": {
            "conclusion": {
                "id": "k1",
                "status": "conditionally_suitable",
                "statement": "현재 근거 범위의 결론",
                "conditions": ["추가 확인 필요"],
                "judgment_ids": ["j1"],
            }
        },
        "S6": {},
        "S7": {"answer": "현재 근거 범위의 결론. 추가 확인 필요."},
    }


@pytest.mark.parametrize("procedure_id", approved_procedures())
def test_every_approved_procedure_completes(procedure_id: str) -> None:
    loaded = procedure(procedure_id)
    case = ReasoningEngine(ScriptedStageLLM(_scripts(loaded.title))).run("요청", procedure_id=procedure_id)

    assert case.status == "completed"
    assert case.procedure_id == procedure_id
    assert len(loaded.steps) == 7
    assert "추가 확인 필요" in case.answer


def test_unapproved_procedure_is_rejected() -> None:
    with pytest.raises(KeyError, match="unapproved procedure"):
        procedure("llm-invented-procedure")


def test_dynamic_three_step_procedure_executes() -> None:
    from nanobot.reasoning.models import Procedure, ProcedureStep

    procedure = Procedure(
        id="dynamic-short",
        version="3.1",
        title="짧은 판단",
        purpose="짧은 판단",
        dynamic=True,
        steps=[
            ProcedureStep(id="D1", role="goal", question="목표", inputs=["request"], outputs=["goal"], checks=["structure"]),
            ProcedureStep(id="D2", role="facts", question="사실", inputs=["D1"], outputs=["facts"], checks=["structure"]),
            ProcedureStep(id="D3", role="answer", question="답변", inputs=["D2"], outputs=["answer"], checks=["faithfulness"]),
        ],
    )
    scripts = {
        "D1": {"goal": "짧은 판단", "constraints": [], "questions": []},
        "D2": {"facts": [{"id": "f1", "proposition": "사실", "source": "request", "status": "confirmed"}]},
        "D3": {"answer": "짧은 판단 결과"},
    }

    case = ReasoningEngine(ScriptedStageLLM(scripts)).run("요청", procedure=procedure)

    assert case.status == "completed"
    assert case.answer == "짧은 판단 결과"
    assert [item for item in case.step_states if case.step_states[item] == "passed"] == ["D1", "D2", "D3"]


def test_stale_revision_result_is_ignored() -> None:
    from nanobot.reasoning.registry import procedure as load_procedure

    class _RevisionLLM(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict) -> dict:
            result = super().complete(stage, payload)
            result["revision"] = 1 if stage == "S1" else 2
            return result

    loaded = load_procedure("method-evaluation")
    loaded.revision = 2
    case = ReasoningEngine(_RevisionLLM(_scripts(loaded.title))).run("요청", procedure=loaded)

    assert case.status == "stopped"
    assert case.history[0]["event"] == "stale_result_ignored"
    assert len(case.history) <= 8


def test_repeated_stale_revision_stops_within_call_budget() -> None:
    class _AlwaysStale:
        def complete(self, stage: str, payload: dict) -> dict:
            return {"revision": 999, "goal": "x"}

    case = ReasoningEngine(_AlwaysStale(), max_stage_calls=4).run("요청")

    assert case.status == "stopped"
    assert case.stop_reason == "stale revision result repeated"
    assert sum(item.get("event") == "stale_result_ignored" for item in case.history) == 2


def test_rule_judgment_records_z3_limit() -> None:
    case = ReasoningEngine(ScriptedStageLLM(_scripts("규칙 판단"))).run("규정", procedure_id="rule-judgment")
    z3_checks = [item for item in case.checks if item.kind == "z3:entailment"]

    assert case.status == "completed"
    assert len(z3_checks) == 1
    assert z3_checks[0].result == "UNDETERMINED"
    assert "not natural-language truth" in z3_checks[0].limit

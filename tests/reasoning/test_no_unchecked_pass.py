"""No step reaches 'passed' without a check that actually ran (design 6.2)."""

from __future__ import annotations

from typing import Any

from nanobot.reasoning.dynamic import validate_dynamic_procedure
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS


def _scripts(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scripts = {key: dict(value) for key, value in STAGE_OUTPUTS.items()}
    scripts.update(overrides)
    return scripts


def test_s6_without_a_check_result_does_not_pass() -> None:
    case = ReasoningEngine(ScriptedStageLLM(_scripts(S6={})), max_revisions=0).run("평가")

    assert case.step_states["S6"] != "passed"
    assert case.status == "stopped"
    assert "semantic check result missing" in case.stop_reason


def test_s6_reported_defect_returns_to_the_conclusion_step() -> None:
    class FlagOnce(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            result = super().complete(stage, payload)
            if stage == "S6" and self.calls.count("S6") == 1:
                result["defects"] = ["결론이 판단 j1의 조건을 빠뜨림"]
            return result

    llm = FlagOnce(_scripts())
    case = ReasoningEngine(llm).run("평가")

    assert case.status == "completed"
    assert llm.calls == ["S1", "S2", "S3", "S4", "S5", "S6", "S5", "S6", "S7"]
    kinds = [item.kind for item in case.checks if item.result == "fail"]
    assert "llm:S6" in kinds


def test_s6_code_check_catches_a_conclusion_citing_unknown_judgments() -> None:
    conclusion = dict(STAGE_OUTPUTS["S5"]["conclusion"], judgment_ids=["j9"])
    case = ReasoningEngine(
        ScriptedStageLLM(_scripts(S5={"conclusion": conclusion})), max_revisions=0
    ).run("평가")

    assert case.step_states["S6"] != "passed"
    assert "unknown judgments" in case.stop_reason


def test_s6_payload_asks_for_the_defects_list() -> None:
    seen: dict[str, Any] = {}

    class Spy(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            seen[stage] = payload
            return super().complete(stage, payload)

    ReasoningEngine(Spy(_scripts())).run("평가")

    assert "defects" in seen["S6"]["expected_outputs"]


def _work_procedure():
    procedure, defects = validate_dynamic_procedure(
        {
            "purpose": "후보 하나를 고른다.",
            "steps": [
                {"id": "check", "question": "후보를 판정한다.", "outputs": ["verdict"]},
                {"id": "answer", "role": "answer", "question": "답한다.", "inputs": ["check"], "outputs": ["answer"]},
            ],
        },
        request="후보 하나를 골라 주세요.",
    )
    assert defects == [] and procedure is not None
    return procedure


def test_work_step_missing_its_declared_output_does_not_pass() -> None:
    llm = ScriptedStageLLM({"check": {"other": "x"}, "answer": {"answer": "B"}})
    case = ReasoningEngine(llm).run("후보 하나를 골라 주세요.", procedure=_work_procedure())

    assert case.step_states["check"] != "passed"
    assert "declared output missing: verdict" in case.stop_reason
    assert "answer" not in llm.calls


def test_every_passed_step_has_a_real_check_record() -> None:
    llm = ScriptedStageLLM({"check": {"verdict": "B 충족"}, "answer": {"answer": "B를 고릅니다."}})
    case = ReasoningEngine(llm).run("후보 하나를 골라 주세요.", procedure=_work_procedure())

    assert case.status == "completed"
    kinds = {item.kind for item in case.checks}
    assert "code:outputs" in kinds
    assert "code:unmapped" not in kinds

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


def test_typed_steps_receive_their_output_shape() -> None:
    seen: dict[str, Any] = {}

    class Spy(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            seen[stage] = payload
            return super().complete(stage, payload)

    ReasoningEngine(Spy(_scripts())).run("평가")

    fact_shape = seen["S2"]["output_shape"]["facts"][0]
    assert set(fact_shape) == {"id", "proposition", "source", "status"}
    assert "linked_goal" in seen["S3"]["output_shape"]["criteria"][0]
    assert "defects" in seen["S6"]["output_shape"]


def test_malformed_typed_output_is_retried_at_the_same_step() -> None:
    """Observed live: S2 returned facts as plain strings and the run looped S1<->S2."""

    class StringFactsOnce(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            result = super().complete(stage, payload)
            if stage == "S2" and self.calls.count("S2") == 1:
                result["facts"] = ["두 일정이 겹침"]
            return result

    llm = StringFactsOnce(_scripts())
    case = ReasoningEngine(llm).run("평가")

    assert case.status == "completed"
    assert llm.calls == ["S1", "S2", "S2", "S3", "S4", "S5", "S6", "S7"]


class _S6Defect(ScriptedStageLLM):
    def __init__(self, scripts, defect: dict[str, Any], times: int = 1) -> None:
        super().__init__(scripts)
        self.defect = defect
        self.times = times

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = super().complete(stage, payload)
        if stage == "S6" and self.calls.count("S6") <= self.times:
            result["defects"] = [dict(self.defect)]
        return result


def test_s6_defect_returns_to_the_step_it_names() -> None:
    """Observed live: S4 evidence defects were sent back to S5, which could not fix them."""

    llm = _S6Defect(_scripts(), {"step": "S4", "issue": "j1 근거가 기준과 무관"})
    case = ReasoningEngine(llm).run("평가")

    assert case.status == "completed"
    assert llm.calls == ["S1", "S2", "S3", "S4", "S5", "S6", "S4", "S5", "S6", "S7"]


def test_s6_defect_with_an_invalid_target_uses_the_default_return() -> None:
    llm = _S6Defect(_scripts(), {"step": "S7", "issue": "x"})
    case = ReasoningEngine(llm).run("평가")

    assert llm.calls == ["S1", "S2", "S3", "S4", "S5", "S6", "S5", "S6", "S7"]
    assert case.status == "completed"


def test_stopped_case_does_not_keep_an_invalidated_answer() -> None:
    """Observed live: a stopped case still carried an answer from an invalidated pass."""

    conclusion = dict(STAGE_OUTPUTS["S5"]["conclusion"], status="conditionally_suitable", conditions=["조건A"])

    class LateFlag(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            result = super().complete(stage, payload)
            if stage == "S7" and self.calls.count("S7") == 1:
                result["answer"] = "조건 없이 답합니다."  # omits 조건A -> back to S5
            if stage == "S6" and self.calls.count("S7") >= 1:
                result["defects"] = [{"step": "S5", "issue": "결론과 판단 불일치"}]
            return result

    llm = LateFlag(_scripts(S5={"conclusion": conclusion}, S7={"answer": "조건A가 충족되면 조정합니다."}))
    case = ReasoningEngine(llm, max_check_returns=10).run("평가")

    assert case.status == "stopped"
    assert case.answer == ""

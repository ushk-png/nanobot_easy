"""Step granularity follows the model's reasoning level (design 1, 10)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from nanobot.reasoning.registry import procedure
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS

COMPACT_OUTPUTS = {
    "S1-2": {**STAGE_OUTPUTS["S1"], **STAGE_OUTPUTS["S2"]},
    "S3-5": {**STAGE_OUTPUTS["S3"], **STAGE_OUTPUTS["S4"], **STAGE_OUTPUTS["S5"]},
    "S6": {"defects": []},
    "S7": STAGE_OUTPUTS["S7"],
}


class Spy(ScriptedStageLLM):
    def __init__(self, scripts) -> None:
        super().__init__(scripts)
        self.payloads: dict[str, dict[str, Any]] = {}

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads[stage] = payload
        return super().complete(stage, payload)


def test_compact_layout_merges_logical_steps() -> None:
    compact = procedure("method-evaluation", "compact")

    assert compact.granularity == "compact"
    assert [step.id for step in compact.steps] == ["S1-2", "S3-5", "S6", "S7"]
    assert compact.step("S1-2").roles == ["goal", "facts"]
    assert compact.step("S3-5").roles == ["criteria", "judgments", "conclusion"]
    assert len(procedure("method-evaluation").steps) == 7


def test_compact_run_uses_four_calls_and_keeps_every_check() -> None:
    llm = Spy(COMPACT_OUTPUTS)
    case = ReasoningEngine(llm).run("평가", procedure=procedure("method-evaluation", "compact"))

    assert case.status == "completed"
    assert llm.calls == ["S1-2", "S3-5", "S6", "S7"]
    kinds = [item.kind for item in case.checks]
    for kind in ("code:S1", "code:S2", "code:S3", "code:S4", "code:S5", "code:S6", "llm:S6", "code:S7"):
        assert kind in kinds
    merged = llm.payloads["S3-5"]
    assert "facts" in merged and "criteria" not in merged
    assert {"criteria", "judgments", "conclusion"} <= set(merged["output_shape"])
    assert [item["id"] for item in llm.payloads["S6"]["fixable_steps"]] == ["S1-2", "S3-5"]


def test_merged_step_defect_is_caught_by_its_role_check() -> None:
    bad = dict(COMPACT_OUTPUTS)
    bad["S3-5"] = {**COMPACT_OUTPUTS["S3-5"], "judgments": []}  # criteria left unjudged
    case = ReasoningEngine(ScriptedStageLLM(bad), max_revisions=0).run(
        "평가", procedure=procedure("method-evaluation", "compact")
    )

    assert case.status == "stopped"
    assert "criteria without judgment" in case.stop_reason


def test_compact_update_reruns_from_the_judgment_step() -> None:
    engine = ReasoningEngine(ScriptedStageLLM(COMPACT_OUTPUTS))
    compact = procedure("method-evaluation", "compact")
    case = engine.run("평가", procedure=compact)
    engine.llm.calls.clear()

    engine.continue_with_facts(
        case, compact, [{"id": "f1", "proposition": "겹치지 않음", "source": "user", "status": "confirmed"}]
    )

    assert engine.llm.calls == ["S3-5", "S6", "S7"]


def test_check_return_cap_turns_repeat_findings_into_open_issues() -> None:
    """Observed live: S6 sent the run back to S4 three times (17 calls)."""

    class AlwaysFlag(Spy):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            result = super().complete(stage, payload)
            if stage == "S6":
                result["defects"] = [{"step": "S3-5", "issue": "D 판단 근거가 약함"}]
            return result

    llm = AlwaysFlag(COMPACT_OUTPUTS)
    case = ReasoningEngine(llm).run("평가", procedure=procedure("method-evaluation", "compact"))

    assert case.status == "completed"
    assert llm.calls == ["S1-2", "S3-5", "S6", "S3-5", "S6", "S7"]
    assert case.step_states["S6"] == "conditionally_passed"
    assert case.open_issues == ["D 판단 근거가 약함"]
    assert llm.payloads["S7"]["open_issues"] == ["D 판단 근거가 약함"]


def test_compact_budget_limits_dynamic_steps() -> None:
    from nanobot.reasoning.dynamic import validate_dynamic_procedure

    steps = [{"id": f"D{i}", "question": f"{i}단계", "outputs": [f"o{i}"]} for i in range(1, 5)]
    steps.append({"id": "D5", "role": "answer", "question": "답한다", "inputs": ["D4"], "outputs": ["answer"]})
    payload = {"purpose": "일정 조정 순서를 판단한다.", "steps": steps}
    from nanobot.reasoning.dynamic import prepare_dynamic_procedure

    procedure_obj, _trace, defects = prepare_dynamic_procedure(payload, request="일정 조정", max_steps=4)
    assert procedure_obj is None
    assert any(item.code == "too_many_steps" for item in defects)
    assert validate_dynamic_procedure(payload, request="일정 조정")[0] is not None


@pytest.mark.auto_granularity
def test_granularity_follows_reasoning_capability(monkeypatch) -> None:
    import nanobot.reasoning.bridge as bridge

    class Off:
        @staticmethod
        def reasoning_disable_supported(model):
            return True

    class AlwaysOn:
        @staticmethod
        def reasoning_disable_supported(model):
            return False

    real = bridge.procedure_granularity
    assert real(object(), "m") == "compact"  # stage effort is 'low'
    monkeypatch.setattr(bridge, "STAGE_REASONING_EFFORT", "none")
    assert real(Off(), "m") == "fine"
    assert real(object(), "m") == "fine"
    assert real(AlwaysOn(), "m") == "compact"


@pytest.mark.auto_granularity
def test_bridge_reuses_the_compact_layout_with_low_effort(tmp_path) -> None:
    from nanobot.reasoning.bridge import _PROCEDURE_PROMPT, run_reasoning_procedure

    class Provider:
        def __init__(self) -> None:
            self.kinds: list[str] = []
            self.compose_body: dict[str, Any] = {}

        async def chat_with_retry(self, **kwargs):
            system = kwargs["messages"][0]["content"]
            body = json.loads(kwargs["messages"][1]["content"])
            if system == _PROCEDURE_PROMPT:
                self.kinds.append("compose")
                self.compose_body = body
                content = {"mode": "reuse", "procedure_id": "method-evaluation"}
            else:
                self.kinds.append(body["stage"])
                content = COMPACT_OUTPUTS[body["stage"]]
            return SimpleNamespace(content=json.dumps(content, ensure_ascii=False), usage={}, finish_reason="stop")

    provider = Provider()
    case = asyncio.run(run_reasoning_procedure(provider, model="m", request="평가해 줘", workspace=tmp_path))

    assert case.status == "completed"
    assert provider.kinds == ["compose", "S1-2", "S3-5", "S6", "S7"]
    assert provider.compose_body["step_budget"] == {"granularity": "compact", "max_steps": 4}
    record = json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])
    assert record["granularity"] == "compact"

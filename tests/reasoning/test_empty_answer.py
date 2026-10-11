"""Empty final answers must not complete a stepwise run (design 4.4, 11).

Observed on 2026-10-11: a dynamic procedure of work steps followed by an
answer step finished with every step "passed" and an empty answer. Each step
call received the same payload (request plus empty case fields and the step
id), without the step's question, expected outputs or earlier step results,
and the model returned an empty ``answer``.
"""

from __future__ import annotations

from typing import Any

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


def test_step_payload_carries_the_step_contract() -> None:
    llm = EchoLLM()
    ReasoningEngine(llm).run(REQUEST, procedure=_selection_procedure())

    payload = llm.payloads["justify"]
    assert payload["question"] == "선정 결과와 근거를 답한다."
    assert payload["expected_outputs"] == ["answer"]


def test_all_steps_passing_with_blank_answer_is_not_completed() -> None:
    case = ReasoningEngine(BlankAnswerLLM()).run(REQUEST, procedure=_selection_procedure())

    assert case.answer.strip() == ""
    assert case.status != "completed"
    assert case.status == "failed_empty_answer"
    assert case.step_states["justify"] != "passed"


def test_procedure_without_answer_output_is_rejected_before_execution() -> None:
    base = {
        "purpose": "조건에 맞는 보고서 하나를 근거와 함께 고른다.",
        "steps": [
            {"id": "check", "question": "후보별 조건 충족을 판정한다.", "outputs": ["checked"]},
            {"id": "pick", "question": "후보 하나를 고른다.", "inputs": ["check"], "outputs": ["selection"]},
        ],
    }
    labelled_only = {
        **base,
        "steps": [
            base["steps"][0],
            {**base["steps"][1], "role": "answer"},
        ],
    }

    for payload in (base, labelled_only):
        procedure, defects = validate_dynamic_procedure(payload, request=REQUEST)
        assert procedure is None
        assert any(item.startswith("answer_step_missing") for item in defects)


def test_normal_procedure_fills_the_answer() -> None:
    case = ReasoningEngine(EchoLLM()).run(REQUEST, procedure=_selection_procedure())

    assert case.status == "completed"
    assert case.answer.startswith("보고서 B")
    assert all(state == "passed" for state in case.step_states.values())


def test_blank_answer_is_retried_once_at_the_answer_step() -> None:
    class BlankOnce(EchoLLM):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(stage)
            result = super().complete(stage, payload)
            if stage == "justify" and "repair_defects" not in payload:
                result["answer"] = ""
            return result

    llm = BlankOnce()
    case = ReasoningEngine(llm).run(REQUEST, procedure=_selection_procedure())

    assert case.status == "completed"
    assert llm.calls == ["check", "filter", "pick", "justify", "justify"]
    assert llm.payloads["justify"]["repair_defects"] == ["answer missing"]


def test_blank_answer_run_is_not_reported_completed_by_the_agent_loop(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from nanobot.agent.loop import AgentLoop
    from nanobot.reasoning.models import ReasoningCase

    async def _fake_run(*_args, **_kwargs):
        return ReasoningCase(
            id="case",
            request="선정",
            procedure_id="dynamic-x",
            procedure_version="3.1",
            status="failed_empty_answer",
            stop_reason="최종 답변 단계(justify)가 답변을 채우지 않아 완료로 처리하지 않았습니다.",
        )

    class _Runtime:
        async def run_status_changed(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _fake_run)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = "none"
    loop.stepwise_enabled = True
    loop.workspace = None
    loop.provider = object()
    loop.model = "xai-grok/grok-4.7"
    loop._runtime_events = lambda: _Runtime()
    ctx = SimpleNamespace(
        msg=SimpleNamespace(content=REQUEST),
        session_key="telegram:test",
        visible_run_started_at=None,
        initial_messages=[],
        final_content=None,
        tools_used=None,
        all_messages=None,
        stop_reason=None,
        had_injections=None,
        on_progress=None,
    )

    asyncio.run(loop._state_run(ctx))

    assert ctx.stop_reason == "failed_empty_answer"
    assert "justify" in ctx.final_content

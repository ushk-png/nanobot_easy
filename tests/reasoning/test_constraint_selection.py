"""Filter-and-select questions: the model extracts, code selects (design 8)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from nanobot.reasoning.selection import constraint_select

REQUIRED = [
    {"attr": "외부통신없음", "op": "==", "value": True},
    {"attr": "감사로그", "op": "==", "value": True},
    {"attr": "일처리량", "op": ">=", "value": 1000},
    {"attr": "도입비_만원", "op": "<=", "value": 5000},
]


def _table(c_volume: int = 1050, d_audit: bool | None = None) -> dict[str, Any]:
    """Genie's question: A-D with D's audit log unconfirmed."""

    def row(cid, audit, volume, cost):
        return {"id": cid, "values": {"외부통신없음": True, "감사로그": audit, "일처리량": volume, "도입비_만원": cost}}

    return {
        "candidates": [
            row("A", False, 1500, 4000),
            row("B", True, 1400, 5200),
            row("C", True, c_volume, 4900),
            row("D", d_audit, 1100, 4700),
        ],
        "required": [dict(item) for item in REQUIRED],
        "objective": {"attr": "일처리량", "direction": "max"},
    }


def test_original_question_selects_c() -> None:
    selection, defects = constraint_select(_table())

    assert defects == []
    assert selection["selected"] == "C"
    candidates = selection["candidates"]
    assert candidates["A"]["status"] == "failed"
    assert candidates["A"]["failed"][0]["condition"].startswith("감사로그")
    assert candidates["B"]["status"] == "failed"
    assert candidates["B"]["failed"][0]["value"] == 5200
    assert candidates["D"]["status"] == "unknown"  # unconfirmed never counts as met


def test_correction_selects_d() -> None:
    """Genie's follow-up: C handles 950/day, D's audit log is confirmed."""

    selection, _ = constraint_select(_table(c_volume=950, d_audit=True))

    assert selection["selected"] == "D"
    assert selection["candidates"]["C"]["status"] == "failed"


def test_no_eligible_candidate_selects_nothing() -> None:
    selection, defects = constraint_select(_table(c_volume=950, d_audit=None))

    assert defects == []
    assert selection["selected"] is None
    assert selection["eligible"] == []


def test_ties_are_reported() -> None:
    table = _table()
    table["candidates"][3]["values"].update({"감사로그": True, "일처리량": 1050})

    selection, _ = constraint_select(table)

    assert selection["selected"] == "C"
    assert selection["ties"] == ["C", "D"]


def test_numbers_written_as_text_are_compared_as_numbers() -> None:
    table = _table()
    table["candidates"][2]["values"]["일처리량"] = "1,050"

    assert constraint_select(table)[0]["selected"] == "C"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda t: t.update(candidates=[]), "no candidates"),
        (lambda t: t["required"][0].update(attr="외부 통신"), "not a candidate value name"),
        (lambda t: t["required"][2].update(op="=>"), "op must be one of"),
        (lambda t: t.update(objective={"attr": "속도", "direction": "max"}), "objective.attr"),
    ],
)
def test_malformed_tables_are_sent_back(mutate, expected) -> None:
    table = _table()
    mutate(table)

    _, defects = constraint_select(table)

    assert any(expected in item for item in defects)


CLAIMS = {"selected": "C", "excluded": ["A", "B", "D"]}


def _scripts(table: dict[str, Any], answer: str) -> dict[str, dict[str, Any]]:
    return {"X1": {"table": table}, "X3": {"answer": answer, "answer_claims": CLAIMS}}


ANSWER = "C를 선정합니다. A는 감사 로그가 없고, B는 도입비가 5,000만 원을 넘으며, D는 감사 로그가 확인되지 않았습니다."


def test_engine_runs_two_model_calls_and_one_code_step() -> None:
    llm = ScriptedStageLLM(_scripts(_table(), ANSWER))
    case = ReasoningEngine(llm).run("선정해 주세요", procedure_id="constraint-selection")

    assert case.status == "completed"
    assert llm.calls == ["X1", "X3"]
    assert case.step_outputs["X2"]["selection"]["selected"] == "C"
    assert case.usage.calls == 2
    assert "code:constraint_select" in [item.kind for item in case.checks]


def test_answer_must_name_the_selection_and_every_other_candidate() -> None:
    class Once(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            result = super().complete(stage, payload)
            if stage == "X3" and self.calls.count("X3") == 1:
                result["answer"] = "C를 선정합니다."
            return result

    llm = Once(_scripts(_table(), ANSWER))
    case = ReasoningEngine(llm).run("선정해 주세요", procedure_id="constraint-selection")

    assert case.status == "completed"
    assert llm.calls == ["X1", "X3", "X3"]
    retry = next(item for item in case.history if item.get("event") == "retry")
    assert "answer omits candidate: A" in retry["defects"]


def test_bad_table_returns_to_extraction() -> None:
    class FixSecond(ScriptedStageLLM):
        def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
            result = super().complete(stage, payload)
            if stage == "X1" and self.calls.count("X1") == 1:
                result["table"] = {**_table(), "required": [{"attr": "x", "op": ">=", "value": 1}]}
            return result

    llm = FixSecond(_scripts(_table(), ANSWER))
    case = ReasoningEngine(llm).run("선정해 주세요", procedure_id="constraint-selection")

    assert case.status == "completed"
    assert llm.calls == ["X1", "X1", "X3"]


@pytest.mark.auto_granularity
def test_bridge_reuse_takes_three_calls_in_total(tmp_path) -> None:
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
                content = {"mode": "reuse", "procedure_id": "constraint-selection"}
            else:
                self.kinds.append(body["stage"])
                content = (
                    {"table": _table()} if body["stage"] == "X1" else {"answer": ANSWER, "answer_claims": CLAIMS}
                )
            return SimpleNamespace(content=json.dumps(content, ensure_ascii=False), usage={}, finish_reason="stop")

    provider = Provider()
    case = asyncio.run(run_reasoning_procedure(provider, model="m", request="선정해 주세요", workspace=tmp_path))

    assert case.status == "completed"
    assert provider.kinds == ["compose", "X1", "X3"]
    catalog = {item["id"]: item for item in provider.compose_body["approved_procedures"]}
    assert "코드로 계산" in catalog["constraint-selection"]["purpose"]


def test_answer_claiming_another_selection_falls_back_to_the_code_template() -> None:
    wrong = {"answer": "D를 선정합니다. A, B, C는 제외합니다.", "answer_claims": {"selected": "D", "excluded": ["A", "B", "C"]}}
    llm = ScriptedStageLLM({"X1": {"table": _table()}, "X3": wrong})
    case = ReasoningEngine(llm).run("선정해 주세요", procedure_id="constraint-selection")

    assert llm.calls == ["X1", "X3", "X3"]
    assert case.status == "completed"
    assert case.answer.startswith("C를 선정합니다.")
    assert "A: 감사로그 == True 미충족(값: False)" in case.answer
    assert "D: 감사로그 미확인" in case.answer
    assert any(item.get("event") == "template_answer" for item in case.history)


def test_template_reports_no_selection_when_nothing_qualifies() -> None:
    from nanobot.reasoning.checks import selection_template_answer
    from nanobot.reasoning.models import ReasoningCase

    selection, _ = constraint_select(_table(c_volume=950, d_audit=None))
    case = ReasoningCase(id="c", request="r", procedure_id="constraint-selection", procedure_version="3.1")
    case.step_outputs["X2"] = {"selection": selection}

    text = selection_template_answer(case)
    assert text.startswith("필수 조건을 확인된 값으로 모두 충족하는 후보가 없어 선정하지 않습니다.")
    assert "C: 일처리량 >= 1000 미충족(값: 950)" in text


def test_answer_starts_with_the_computed_result() -> None:
    llm = ScriptedStageLLM(_scripts(_table(), "확인된 조건을 모두 충족하는 제품은 C뿐입니다. A, B, D는 제외됩니다."))
    case = ReasoningEngine(llm).run("선정해 주세요", procedure_id="constraint-selection")

    assert case.answer.startswith("선정: C\n")

    none_claims = {"selected": None, "excluded": ["A", "B", "C", "D"]}
    llm = ScriptedStageLLM(
        {"X1": {"table": _table(c_volume=950)}, "X3": {"answer": "모두 조건 미달입니다. A B C D", "answer_claims": none_claims}}
    )
    case = ReasoningEngine(llm).run("선정해 주세요", procedure_id="constraint-selection")
    assert case.answer.startswith("선정 결과: 필수 조건을 확인된 값으로 모두 충족하는 후보가 없어 선정하지 않습니다.")

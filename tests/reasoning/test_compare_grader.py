"""The comparison grader reads real answer styles as intended (rules fixed before runs)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "scripts" / "stepwise_compare"
sys.path.insert(0, str(ROOT))

from grade import grade, selected_label, verdict_label  # noqa: E402

CASES = {item["id"]: item for item in json.loads((ROOT / "cases.json").read_text())["cases"]}


def test_selection_labels_from_observed_answers() -> None:
    ids = ["A", "B", "C", "D"]
    assert selected_label(
        "필수 조건을 모두 충족한 C를 선정한다. A는 감사 로그가 없고, B는 도입비가 5,200만 원으로 한도를 넘는다. "
        "D는 감사 로그 제공 여부가 미확인이라 선정하지 않는다.",
        ids,
    ) == "C"
    assert selected_label("선정 결과는 D입니다. A는 감사 로그가 없어 탈락했습니다. C는 950건으로 미달입니다.", ids) == "D"
    assert selected_label("C를 선정합니다.\nA: 감사로그 == True 미충족(값: False).", ids) == "C"
    assert selected_label("필수 조건을 확인된 값으로 모두 충족하는 후보가 없어 선정하지 않습니다.", ["K", "L", "M"]) == "none"
    assert selected_label("C와 D가 후보입니다. C를 선정합니다. D를 선정합니다.", ids) == "ambiguous"


def test_verdict_labels() -> None:
    assert verdict_label("외부 API를 쓰므로 내부망 조건을 충족하지 못해 부적합합니다.") == "unsuitable"
    assert verdict_label("필수 조건이 미확인이라 적합 여부는 보류합니다.") == "conditional"
    assert verdict_label("필수 조건을 모두 충족해 적합합니다. 비용은 선호 조건을 넘습니다.") == "suitable"


def test_grade_flags_unknown_promoted_and_violations() -> None:
    tools = CASES["sel_tools"]
    picked_d = grade(tools, "D를 선정합니다. D는 감사 로그를 제공합니다.")
    assert picked_d["correct"] is False
    assert picked_d["unknown_as_confirmed"] is True

    picked_b = grade(tools, "처리량이 큰 B를 선정합니다.")
    assert picked_b["required_violation"] is True

    good = grade(
        tools,
        "C를 선정합니다. A는 감사 로그가 없고, B는 도입비가 한도를 넘고, D는 감사 로그 제공 여부가 확인되지 않았습니다.",
        {"selected": "C"},
    )
    assert good["correct"] and good["reasons_complete"] and good["consistent"]
    assert good["unknown_as_confirmed"] is False

    suitable = CASES["eval_suitable"]
    offset = grade(suitable, "월 비용이 선호 조건을 넘어 부적합합니다.", {"conclusion_status": "unsuitable"})
    assert offset["required_violation"] is True and offset["consistent"] is True

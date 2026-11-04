"""t60: two local contrast tests, MOCK provider (no network, no real model call).

Labels:
REAL-RECORD REPLAY - the t56 A1/A2 raw designs and recorded A1 reply (ids remapped only).
MOCK SUPPLEMENT    - an executor step / design the records do not contain.
Test code never edits requirements or verification state of a running turn.
"""
import json

import pytest

from .t51_harness import Session, design, final
from .test_context_t58_local import a1_step, a2_step, record
from .test_context_t59_local import calc_args, supplement, tool_results


# 1 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_t60_1_stale_previous_total_in_spec_and_answer_does_not_complete(tmp_path, monkeypatch):
    """REAL-RECORD REPLAY (A1 turn, A2 design) + MOCK SUPPLEMENT: the executor states the calculation
    with the A1 total (60, cited from the verified A1 original) and answers with the A1 60-minute plan.
    The current request says 총 45분: the stale value must not satisfy the current requirement."""
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(a1_step)
    assert await s.turn(record(1)["request"]) == record(1)["reply"]
    stale_answer = record(1)["reply"]                              # 20 + 20 + 20 = 60분
    stale = calc_args(total={"value": 60, "source_span": "총 60분 연습 계획을 만들어 주세요."})
    s.mock.designs.append(a2_step())
    s.mock.steps.extend([final(stale_answer), supplement(stale), final(stale_answer)])
    out = await s.turn(record(2)["request"])
    sel = s.selected[-1]
    assert "총 45분" in sel.request_text and "총 60분" not in sel.request_text
    assert not sel.calc_supplements or all(v["spec"].expected_values["total"] != 60
                                           for v in sel.calc_supplements.values())
    assert any("superseded" in r for r in tool_results(s))         # rejected with the reason
    assert sel.completion_supported() is False
    assert out.startswith("요청을 완료하지 못했습니다")
    assert [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]


# 2 ------------------------------------------------------------------------------------------------
REQ2 = ("퍼팅, 어프로치, 아이언으로 총 45분 연습 계획을 만들어 주세요. "
        "퍼팅 20분, 어프로치 15분, 아이언 15분으로 해 주세요. 인터넷 검색은 하지 마세요.")


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "총 45분 계획: 퍼팅 20분, 어프로치 15분, 아이언 15분입니다.",          # repeats the contradiction
    "총 45분 계획: 퍼팅 20분, 어프로치 15분, 아이언 10분입니다.",          # silently changes a condition
])
async def test_t60_2_contradictory_user_numbers_are_not_passed_as_calc_not_needed(tmp_path, monkeypatch, answer):
    """MOCK SUPPLEMENT (constructed design): every number is the user's own, but 20+15+15=50≠45.
    Must not complete; conditions unchanged; the contradiction is named and confirmation is asked."""
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(REQ2, design(
        goal="45분 연습 계획", route="direct", answer=answer,
        completion_checks=["총 45분, 퍼팅 20분, 어프로치 15분, 아이언 15분이다", "드라이버 없이 검색하지 않았다"],
        obligations={"target": "answer", "lookup": "forbidden", "external_change": "none",
                     "source_span": "인터넷 검색은 하지 마세요."}))
    sel = s.selected[-1]
    assert sel.request_text == REQ2                                 # conditions not altered
    assert sel.calc_spec_missing() == []                            # all numbers are the user's own ...
    assert sel.completion_supported() is False                      # ... but that is not "pass"
    assert out != answer and "확인이 필요합니다" in out
    assert "총 45분" in out and "50" in out                          # names the conflicting requirements
    assert [t["status"] for t in s.tasks()] != ["DONE"]

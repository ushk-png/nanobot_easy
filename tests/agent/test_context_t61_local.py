"""t61 T01-T18: current-valid requirements, first/supplement spec value check, quantity relations.

MOCK provider only (no network, no real model call). Labels per test:
REAL-RECORD REPLAY - t56 A1/A2 raw designs / recorded texts (ids remapped only).
MOCK               - constructed designs, answers, supplements.
Test code never edits requirements or verification state of a running turn.
"""
import json

import pytest

from .t51_harness import Session, design, design_response, final
from .test_context_t58_local import SPEC, a1_step, a2_step, executor_text, record
from .test_context_t59_local import calc_args, fix_messages, supplement
from . import test_context_t60_local as t60

ASK = "확인이 필요합니다"
NO_SEARCH = {"target": "answer", "lookup": "unneeded", "external_change": "none"}


def rel(sel):
    """Relation results computed in code (absent on the baseline -> [])."""
    return getattr(sel, "relation_checks", lambda: [])()


def statuses(sel):
    return [r["status"] for r in rel(sel)]


async def one(tmp_path, monkeypatch, req, answer, checks, **kw):
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(req, design(goal="계획", route="direct", answer=answer, completion_checks=checks,
                                   obligations=NO_SEARCH, **kw))
    return s, out, s.selected[-1]


def done(s):
    return [t["status"] for t in s.tasks()] == ["DONE"]


def asked(out):
    return out.startswith(ASK)


# T01 / T02 / T03 : current value vs first-design spec and supplement spec (same check) ---------------
STALE_SPEC = {"values": {"퍼팅": 20, "어프로치": 20, "아이언": 20}, "total": 60, "unit": "분"}


@pytest.mark.asyncio
async def test_T01_first_design_spec_with_stale_60_rejected_bounded_rewrite_no_reask(tmp_path, monkeypatch):
    """REAL-RECORD REPLAY (A1, A2 design) + MOCK (design spec built on the A1 total 60, stale answer,
    one correct supplement, corrected answer)."""
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(a1_step)
    await s.turn(record(1)["request"])
    s.mock.designs.append(a2_step(spec=STALE_SPEC))
    s.mock.steps.extend([final(record(1)["reply"]), supplement(calc_args()), final(executor_text())])
    out = await s.turn(record(2)["request"])
    sel = s.selected[-1]
    assert sel.request_text == record(2)["request"]                    # original kept
    rej = getattr(sel, "spec_rejections", lambda: {})()
    assert rej and all(any("current_requirement" in x for x in v) for v in rej.values())  # the 60-spec is not adopted
    msgs = fix_messages(s)
    assert len(msgs) == 1 and "rejected" in msgs[0]                   # bounded rewrite, reason given
    assert not any(ASK in str(m.get("content")) for r in s.executor_requests() for m in r["messages"])
    assert not asked(out) and out == executor_text() and done(s)       # no 45-vs-60 question


@pytest.mark.asyncio
async def test_T01b_stale_first_spec_budget_exhausted_reports_incomplete(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(a1_step)
    await s.turn(record(1)["request"])
    s.loop.max_iterations = 1
    s.mock.designs.append(a2_step(spec=STALE_SPEC))
    s.mock.steps.append(final(record(1)["reply"]))
    out = await s.turn(record(2)["request"])
    assert out.startswith("요청을 완료하지 못했습니다") and not asked(out)
    assert [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]


@pytest.mark.asyncio
async def test_T02_supplement_spec_with_stale_60_rejected_as_in_t60(tmp_path, monkeypatch):
    await t60.test_t60_1_stale_previous_total_in_spec_and_answer_does_not_complete(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_T03_first_spec_45_and_conditions_met_done(tmp_path, monkeypatch):
    """REAL-RECORD REPLAY + MOCK (design spec 45, the recorded A2 answer)."""
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(a1_step)
    await s.turn(record(1)["request"])
    s.mock.designs.append(a2_step(spec=SPEC))
    s.mock.steps.append(final(executor_text()))
    out = await s.turn(record(2)["request"])
    sel = s.selected[-1]
    assert out == executor_text() and done(s) and fix_messages(s) == []
    assert getattr(sel, "spec_rejections", lambda: {})() == {}
    assert "pass" in statuses(sel)                                     # 45 - 20 = 25 >= 0 (remainder)


# T04 ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_T04_other_schedule_60_and_current_45_not_mixed(tmp_path, monkeypatch):
    req = ("어제 수영 일정은 총 60분이었습니다. 오늘 골프 연습은 퍼팅, 어프로치, 아이언으로 총 45분이고 "
           "퍼팅 20분, 어프로치 15분, 아이언 10분으로 짜 주세요.")
    ans = "오늘 골프 연습 총 45분: 퍼팅 20분, 어프로치 15분, 아이언 10분."
    s, out, sel = await one(tmp_path, monkeypatch, req, ans, ["총 45분: 퍼팅 20분, 어프로치 15분, 아이언 10분"],
                            completion_specs=[{"check": "총 45분: 퍼팅 20분, 어프로치 15분, 아이언 10분",
                                               "result_kind": "computed", "expected_values": {
                                                   "values": {"퍼팅": 20, "어프로치": 15, "아이언": 10},
                                                   "total": 45, "unit": "분"}}])
    assert out == ans and done(s) and not asked(out)
    assert "conflict" not in statuses(sel) and "pass" in statuses(sel)


# T05 / T06 / T07 : full composition --------------------------------------------------------------
REQ = "퍼팅, 어프로치, 아이언으로 총 45분 연습 계획을 만들어 주세요. 퍼팅 20분, 어프로치 {a}분, 아이언 {b}분으로 해 주세요."


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["총 45분 계획: 퍼팅 20분, 어프로치 15분, 아이언 15분입니다.",
                                    "퍼팅 20분 → 어프로치 15분 → 아이언 10분, 모두 45분입니다."])
async def test_T05_composition_50_vs_45_conflict_asks_and_blocks_change(tmp_path, monkeypatch, answer):
    req = REQ.format(a=15, b=15)
    s, out, sel = await one(tmp_path, monkeypatch, req, answer, ["계획을 제시했다"])
    assert sel.request_text == req and asked(out) and not done(s)
    assert "45분" in out and "50분" in out                              # names both sides
    assert "conflict" in statuses(sel)
    assert sel.mutation_gate("write_file", "plan.md") is not None       # dependent change held


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["총 45분 계획: 퍼팅 20분, 어프로치 15분, 아이언 10분입니다.",
                                    "퍼팅부터 20분, 이어서 어프로치 15분, 마지막 아이언 10분(합계 45분)."])
async def test_T06_composition_matches_done(tmp_path, monkeypatch, answer):
    s, out, sel = await one(tmp_path, monkeypatch, REQ.format(a=15, b=10), answer, ["계획을 제시했다"])
    assert out == answer and done(s) and statuses(sel) == ["pass"]


@pytest.mark.asyncio
async def test_T07_composition_short_35_vs_45_conflict(tmp_path, monkeypatch):
    req = "퍼팅, 어프로치, 아이언으로 총 45분 연습 계획을 만들어 주세요. 퍼팅 15분, 어프로치 10분, 아이언 10분으로 해 주세요."
    s, out, sel = await one(tmp_path, monkeypatch, req, "계획: 15/10/10분", ["계획을 제시했다"])
    assert asked(out) and "35분" in out and not done(s) and "conflict" in statuses(sel)


# T08 upper bound ---------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_T08_upper_bound_with_slack_done_no_question(tmp_path, monkeypatch):
    req = ("45분 이내로 퍼팅, 어프로치, 아이언 연습 계획을 짜 주세요. 퍼팅 15분, 어프로치 10분, 아이언 10분이고 "
           "남는 시간은 여유로 두세요.")
    ans = "퍼팅 15분, 어프로치 10분, 아이언 10분(35분), 남는 10분은 여유입니다."
    s, out, sel = await one(tmp_path, monkeypatch, req, ans, ["계획을 제시했다"])
    assert out == ans and done(s) and statuses(sel) == ["pass"]
    assert rel(sel)[0]["kind"] == "upper_bound"


# T09 / T10 / T11 remainder ------------------------------------------------------------------------
REM = "총 45분 연습 계획을 짜 주세요. 퍼팅 {a}분, 어프로치 {b}분으로 하고 나머지는 복습으로 해 주세요.{extra}"


@pytest.mark.asyncio
async def test_T09_remainder_negative_conflict_asks(tmp_path, monkeypatch):
    s, out, sel = await one(tmp_path, monkeypatch, REM.format(a=30, b=20, extra=""), "계획", ["계획을 제시했다"])
    assert asked(out) and not done(s) and "conflict" in statuses(sel) and "50분" in out


@pytest.mark.asyncio
async def test_T10_remainder_10_done(tmp_path, monkeypatch):
    ans = "퍼팅 20분, 어프로치 15분, 복습 10분."
    s, out, sel = await one(tmp_path, monkeypatch, REM.format(a=20, b=15, extra=""), ans, ["계획을 제시했다"])
    assert out == ans and done(s) and statuses(sel) == ["pass"] and rel(sel)[0]["remainder"] == "10"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,ok", [("", True), (" 복습은 최소 5분으로 해 주세요.", False)])
async def test_T11_remainder_zero_allowed_unless_minimum(tmp_path, monkeypatch, extra, ok):
    s, out, sel = await one(tmp_path, monkeypatch, REM.format(a=25, b=20, extra=extra), "퍼팅 25분, 어프로치 20분.",
                            ["계획을 제시했다"])
    if ok:
        assert done(s) and statuses(sel) == ["pass"] and not asked(out)
    else:
        assert asked(out) and not done(s) and "conflict" in statuses(sel)


# T12 relation in requirement record, omitted from the spec ---------------------------------------
@pytest.mark.asyncio
async def test_T12_relation_runs_without_calc_flag_or_spec(tmp_path, monkeypatch):
    s, out, sel = await one(tmp_path, monkeypatch, REQ.format(a=15, b=15), "계획입니다.", ["답변을 제시했다"])
    assert sel.calc_spec_missing() == [] and sel.design.completion_specs == []
    assert "conflict" in statuses(sel) and asked(out) and not done(s)
    assert sel.trace.counters.get("relation_checks", 0) >= 1


# T13 unclear scope, essential --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_T13_unclear_scope_stays_unverified_asks_scope_only(tmp_path, monkeypatch):
    req = "총 45분 중에 퍼팅 20분, 어프로치 15분을 넣어서 계획을 짜 주세요."
    s, out, sel = await one(tmp_path, monkeypatch, req, "퍼팅 20분, 어프로치 15분.", ["총 45분 계획이다"])
    assert statuses(sel) == ["insufficient_info"] and not done(s)
    assert asked(out) and "포함" in out and "모순" not in out


# T14 unrelated numbers -----------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("req", [
    "다음 문장을 영어로 번역해 주세요: '총 45분 중 퍼팅 20분, 어프로치 15분, 아이언 15분'",
    "예를 들어 총 30분, 걷기 10분, 뛰기 10분 같은 계획표 형식이 무엇인지 설명해 주세요.",
    "회의는 3시에 2층에서 한다는 문장을 정중하게 바꿔 주세요."])
async def test_T14_quotes_examples_plain_answers_complete(tmp_path, monkeypatch, req):
    ans = "답변입니다."
    s, out, sel = await one(tmp_path, monkeypatch, req, ans, ["요청한 형태로 답했다"])
    assert out == ans and done(s) and "conflict" not in statuses(sel)


# T15 / T18 follow-ups ----------------------------------------------------------------------------
def followup(req_span, answer, checks=("계획을 제시했다",)):
    def step(messages):
        p = json.loads(messages[1]["content"])
        prev = next(x["event_id"] for x in p["sources"]
                    if x["event_type"] == "USER_MESSAGE" and x.get("content_ref") != "request")
        d = design(goal="계획 수정", route="direct", answer=answer, task_relation="change",
                   task_id=p["prior_context"]["current_task_id"], completion_checks=list(checks),
                   constraints=[{"text": req_span, "source_ids": [prev]}], obligations=NO_SEARCH)
        return design_response(d)
    return step


def first_turn(req, answer):
    def step(messages):
        p = json.loads(messages[1]["content"])
        rid = next(x["event_id"] for x in p["sources"] if x.get("content_ref") == "request")
        return design_response(design(goal="계획", route="direct", answer=answer, completion_checks=["계획을 제시했다"],
                                      constraints=[{"text": req, "source_ids": [rid]}], obligations=NO_SEARCH))
    return step


@pytest.mark.asyncio
async def test_T15_user_picks_total_50_only_that_updates_then_done(tmp_path, monkeypatch):
    req = REQ.format(a=15, b=15)
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(first_turn(req, "계획"))
    out1 = await s.turn(req)
    assert asked(out1)
    ans = "총 50분: 퍼팅 20분, 어프로치 15분, 아이언 15분."
    s.mock.designs.append(followup(req, ans))
    out2 = await s.turn("총시간을 50분으로 해 주세요.")
    sel = s.selected[-1]
    assert out2 == ans and statuses(sel) == ["pass"] and [t["status"] for t in s.tasks()][-1] == "DONE"
    assert rel(sel)[0]["total"] == "50" and rel(sel)[0]["superseded"] == {"total": "45"}


@pytest.mark.asyncio
async def test_T18_pass_then_requirement_change_rechecked(tmp_path, monkeypatch):
    req = REQ.format(a=15, b=10)
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(first_turn(req, "총 45분: 20/15/10"))
    out1 = await s.turn(req)
    assert out1 == "총 45분: 20/15/10" and statuses(s.selected[-1]) == ["pass"]
    s.mock.designs.append(followup(req, "총 45분: 20/15/15"))
    out2 = await s.turn("아이언은 15분으로 바꿔 주세요.")
    sel = s.selected[-1]
    assert "conflict" in statuses(sel) and asked(out2)               # the earlier pass is not reused
    assert [t["status"] for t in s.tasks()][-1] != "DONE"


# T16 structured vs raw text ------------------------------------------------------------------------
POLITE = ("골프 연습 계획을 부탁드려도 될까요? 시간은 총 45분이면 좋겠어요. 퍼팅, 어프로치, 아이언 세 가지로만 "
          "채워 주시면 감사하겠습니다. 퍼팅은 20분, 어프로치는 15분, 아이언도 15분으로 부탁드립니다.")
STRUCT = {"kind": "composition", "kind_span": "퍼팅, 어프로치, 아이언 세 가지로만 채워 주시면", "unit": "분",
          "total": {"value": 45, "source_span": "시간은 총 45분이면 좋겠어요."},
          "items": [{"label": "퍼팅", "value": 20, "source_span": "퍼팅은 20분"},
                    {"label": "어프로치", "value": 15, "source_span": "어프로치는 15분"},
                    {"label": "아이언", "value": 15, "source_span": "아이언도 15분으로"}]}


@pytest.mark.asyncio
async def test_T16a_structured_relation_distributed_polite_same_verdict(tmp_path, monkeypatch):
    s, out, sel = await one(tmp_path, monkeypatch, POLITE, "계획", ["계획을 제시했다"], quantity_relations=[STRUCT])
    assert "conflict" in statuses(sel) and asked(out) and "50분" in out and not done(s)


@pytest.mark.asyncio
async def test_T16b_raw_text_distributed_polite_is_unresolved_not_success(tmp_path, monkeypatch):
    """The exact composition is still unknown, but 50>45 contradicts both allowed readings."""
    s, out, sel = await one(tmp_path, monkeypatch, POLITE, "계획", ["총 45분 계획이다"])
    assert statuses(sel) == ["conflict"] and not done(s) and "pass" not in statuses(sel)


@pytest.mark.asyncio
async def test_T16c_structured_model_kind_without_user_span_is_not_trusted(tmp_path, monkeypatch):
    bad = {**STRUCT, "kind_span": "모두 합쳐 45분이 되게"}                 # not in the user text
    s, out, sel = await one(tmp_path, monkeypatch, POLITE, "계획", ["총 45분 계획이다"], quantity_relations=[bad])
    structured = [r for r in rel(sel) if r["origin"] == "structured"]
    assert all(r["kind"] == "unclear" for r in structured)  # unsupported model kind stays untrusted
    assert all(r["status"] == "conflict" for r in structured)  # independently, items exceed the total
    assert not done(s)


# T17 nested / parallel ----------------------------------------------------------------------------
NEST = "총 45분 연습 계획을 짜 주세요. 숏게임 25분(그중 퍼팅 15분, 칩샷 10분), 아이언 20분으로 해 주세요."


@pytest.mark.asyncio
async def test_T17a_structured_parent_child_not_double_counted_done(tmp_path, monkeypatch):
    st = {"kind": "composition", "kind_span": "숏게임 25분(그중 퍼팅 15분, 칩샷 10분), 아이언 20분으로", "unit": "분",
          "total": {"value": 45, "source_span": "총 45분 연습 계획을 짜 주세요."},
          "items": [{"label": "숏게임", "value": 25, "source_span": "숏게임 25분"},
                    {"label": "퍼팅", "value": 15, "source_span": "퍼팅 15분", "parent": "숏게임"},
                    {"label": "칩샷", "value": 10, "source_span": "칩샷 10분", "parent": "숏게임"},
                    {"label": "아이언", "value": 20, "source_span": "아이언 20분으로"}]}
    ans = "숏게임 25분(퍼팅 15, 칩샷 10), 아이언 20분."
    s, out, sel = await one(tmp_path, monkeypatch, NEST, ans, ["계획을 제시했다"], quantity_relations=[st])
    assert out == ans and done(s) and [r["status"] for r in rel(sel) if r["origin"] == "structured"] == ["pass"]
    assert "conflict" not in statuses(sel)


@pytest.mark.asyncio
async def test_T17b_raw_text_nested_not_conflicted(tmp_path, monkeypatch):
    s, out, sel = await one(tmp_path, monkeypatch, NEST, "계획", ["계획을 제시했다"])
    assert "conflict" not in statuses(sel) and "모순" not in out

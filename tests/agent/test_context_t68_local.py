"""t68: D1 minimal fix - the final answer is compared in code with the time relations already identified
in the current valid requirements, even when the design's completion_checks is empty.

REPLAY (no-send): tests/agent/fixtures/t67/u3_t65_design.json, the design the real model sent in t65
test 3. MOCK: every other design / executor answer. Every test asserts zero network sends and counts
the mock provider's calls (design + executor).
"""
import pytest

from nanobot.agent.context_selection import relation_answer_finding, requirement_text_groups, evaluate_quantity_group

from .t51_harness import Session, final
from .test_context_t58_local import submit
from .test_context_t67_local import BASE, done, no_network, replay_u3  # noqa: F401  (autouse no-send guard)

REQ = ("45분 이내로 퍼팅, 어프로치, 아이언 연습 계획을 짜 주세요. 퍼팅 15분, 어프로치 10분, 아이언 10분이고 "
       "남는 시간은 여유로 두세요.")
REL = [{"kind": "upper_bound", "kind_span": "45분 이내"}]
OK = "퍼팅 15분, 어프로치 10분, 아이언 10분으로 지정 연습 35분, 남는 10분은 여유입니다."
SLACK15 = "퍼팅 15분, 어프로치 10분, 아이언 10분, 여유 15분입니다."
FIXED = "퍼팅 20분, 어프로치 10분, 아이언 10분, 여유 5분입니다."
OLD60 = "총 60분 계획: 퍼팅 15분, 어프로치 10분, 아이언 10분, 여유 25분입니다."
NO_NUM = "퍼팅, 어프로치, 아이언 순서로 연습하고 남는 시간은 여유로 둡니다."


def finding(answer, req=REQ):
    out = [f for g in requirement_text_groups(req) if (f := relation_answer_finding(evaluate_quantity_group(g), answer))]
    assert len(out) <= 1
    return out[0] if out else None


async def run(tmp_path, monkeypatch, design, steps=(), req=REQ):
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(design)
    s.mock.steps.extend(steps)
    out = await s.turn(req)
    return s, out, s.selected[-1]


def direct(answer):
    return submit({**BASE, "answer": answer, "quantity_relations": REL})


def execute():
    return submit({**BASE, "route": "execute", "quantity_relations": REL})


# --- code-level comparison: items, total and remainder linked --------------------------------------
def test_u3_replay_answer_wording_links_15_10_10_35_45_10():
    req, _ = replay_u3()
    import json
    from .test_context_t67_local import FIX
    ans = json.loads(FIX.read_text())["design_arguments"]["answer"]
    f = finding(ans, req)
    assert f["status"] == "pass"
    assert f["answer_items"] == {"퍼팅": ["15"], "어프로치": ["10"], "아이언": ["10"]}
    assert (f["sum"], f["total"], f["expected_remainder"]) == ("35", "45", "10")
    assert set(f["answer_remainder"]) == {"10"} and set(f["answer_totals"]) <= {"45", "35"}


@pytest.mark.parametrize("answer,status,reason", [
    (OK, "pass", None),
    (SLACK15, "error", "remainder_wrong:15 != 45-35=10"),
    (FIXED, "error", "fixed_item_changed:퍼팅 20 != 15"),
    (OLD60, "error", "total_changed:60 != 45"),
    (NO_NUM, "undecidable", "remainder"),
])
def test_finding_contrast(answer, status, reason):
    f = finding(answer)
    assert f["status"] == status
    if reason:
        assert any(r.startswith(reason) for r in f["reasons"]), f["reasons"]


def test_old_total_also_flags_derived_remainder():
    assert "remainder_wrong:25 != 45-35=10" in finding(OLD60)["reasons"]


def test_no_false_positive_quote_and_separate_schedule():
    quoted = OK + ' 메모에 적힌 "퍼팅 20분, 여유 30분"은 이전 안입니다.'
    sched = OK + "\n내일 회의는 60분, 점심은 30분입니다."
    assert finding(quoted)["status"] == "pass"
    assert finding(sched)["status"] == "pass"
    assert finding("TCP는 연결 지향입니다.", "TCP와 UDP 차이를 설명해 주세요.") is None


# --- end to end, completion_checks == [] in every design -------------------------------------------
@pytest.mark.asyncio
async def test_e2e_correct_replay_done_no_extra_call(tmp_path, monkeypatch):
    """REPLAY of the real t65 test-3 design: DONE, verified by relation, 1 call (the design) only."""
    req, step = replay_u3()
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(step)
    out = await s.turn(req)
    sel = s.selected[-1]
    assert sel.design.completion_checks == []
    assert done(s) and len(s.mock.requests) == 1
    rep = sel.calculation_report()
    assert rep["answer_values"] == "verified_by_relation" and rep["overall"] == "pass"


@pytest.mark.asyncio
async def test_e2e_correct_direct_done_one_call(tmp_path, monkeypatch):
    s, out, sel = await run(tmp_path, monkeypatch, direct(OK))
    assert done(s) and len(s.mock.requests) == 1 and out == OK


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong,code", [(SLACK15, "remainder_wrong"), (FIXED, "fixed_item_changed"),
                                        (OLD60, "total_changed")])
async def test_e2e_direct_error_rerouted_to_executor_then_done(tmp_path, monkeypatch, wrong, code):
    """A clear numeric error in a direct answer: validator-class reroute (no repair call, no question);
    the executor writes the answer under the normal checks."""
    s, out, sel = await run(tmp_path, monkeypatch, direct(wrong), steps=[final(OK)])
    assert sel.design.route == "execute" and sel.design.completion_checks == []
    assert any(e.startswith(code) for e in (sel.reinterpretation or {}).get("answer_relation_errors", []))
    assert done(s) and out == OK and len(s.mock.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [SLACK15, FIXED, OLD60])
async def test_e2e_executor_error_uses_bounded_fix_then_done(tmp_path, monkeypatch, wrong):
    s, out, sel = await run(tmp_path, monkeypatch, execute(), steps=[final(wrong), final(OK)])
    assert "answer_check_mismatch" in sel.trace.reasons
    assert done(s) and out == OK and len(s.mock.requests) == 3   # design + answer + one fix


@pytest.mark.asyncio
async def test_e2e_executor_error_persisting_is_not_done(tmp_path, monkeypatch):
    s, out, sel = await run(tmp_path, monkeypatch, execute(), steps=[final(SLACK15), final(SLACK15)])
    assert not done(s) and len(s.mock.requests) == 3
    assert not out.startswith("확인이 필요합니다")   # never turned into a user question


@pytest.mark.asyncio
async def test_e2e_correct_executor_no_extra_call(tmp_path, monkeypatch):
    s, out, sel = await run(tmp_path, monkeypatch, execute(), steps=[final(OK)])
    assert done(s) and len(s.mock.requests) == 2


@pytest.mark.asyncio
async def test_e2e_undecidable_not_done_no_call_no_question(tmp_path, monkeypatch):
    s, out, sel = await run(tmp_path, monkeypatch, direct(NO_NUM))
    assert not done(s) and len(s.mock.requests) == 1
    assert sel.relation_answer_unverified() and not out.startswith("확인이 필요합니다")
    assert sel.calculation_report()["answer_values"] == "unverified"


@pytest.mark.asyncio
async def test_e2e_general_answer_unaffected(tmp_path, monkeypatch):
    s, out, sel = await run(tmp_path, monkeypatch, submit({**BASE, "answer": "TCP는 연결 지향, UDP는 비연결입니다."}),
                            req="TCP와 UDP 차이를 설명해 주세요.")
    assert done(s) and len(s.mock.requests) == 1 and sel.relation_answer_findings() == []


@pytest.mark.asyncio
async def test_e2e_quote_and_separate_schedule_done(tmp_path, monkeypatch):
    ans = OK + ' 메모의 "퍼팅 20분"은 이전 안입니다. 내일 회의는 60분입니다.'
    s, out, sel = await run(tmp_path, monkeypatch, direct(ans))
    assert done(s) and len(s.mock.requests) == 1


# --- remainder allocated to named items (t65 S1 wording) -------------------------------------------
REQ_S1 = ("퍼팅, 어프로치, 아이언으로 총 60분 연습 계획을 만들어 주세요. 퍼팅은 20분이고, 나머지 시간은 어프로치와 "
          "아이언에 똑같이 나눠 주세요.")


@pytest.mark.parametrize("answer,status,reason", [
    ("총 60분: 퍼팅 20분, 어프로치 20분, 아이언 20분.", "pass", None),
    ("총 60분: 퍼팅 20분, 어프로치 25분, 아이언 20분.", "error", "remainder_wrong:25+20 != 60-20=40"),
    ("총 60분: 퍼팅 20분, 어프로치 30분, 아이언 10분.", "error", "remainder_not_equal"),
    ("총 60분: 퍼팅 20분, 어프로치와 아이언은 적당히.", "undecidable", "remainder_item:"),
])
def test_remainder_to_named_items(answer, status, reason):
    f = finding(answer, REQ_S1)
    assert f["status"] == status
    if reason:
        assert any(r.startswith(reason) for r in f["reasons"]), f["reasons"]

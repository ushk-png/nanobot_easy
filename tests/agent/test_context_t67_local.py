"""t67: the calculation report shows each numeric basis separately (relation checks of the user's
requirements / computed-spec checks of the answer / whether the answer's own numbers were checked),
taken from the completion verdict's own results. Reporting only: completion behaviour is unchanged.

REPLAY (no-send): tests/agent/fixtures/t67/u3_t65_design.json is the design the real model sent in t65
test 3 (raw user text + adopted function arguments only). MOCK: the constructed designs of (a)-(d).
Every test asserts zero network sends.
"""
import json
from pathlib import Path

import httpx
import pytest

from .t51_harness import Session
from .test_context_t58_local import submit

FIX = Path(__file__).parent / "fixtures" / "t67" / "u3_t65_design.json"
NO_SEARCH = {"target": "answer", "lookup": "unneeded", "external_change": "none"}
BASE = dict(goal="답변", request_type="other", task_relation="new", task_id=None, route="direct",
            constraints=[], information_needs=[], capabilities_needed=[], completion_checks=[],
            unresolved=[], answer=None, obligations=NO_SEARCH)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    sends = []

    async def refuse(self, request, *a, **k):
        sends.append(str(request.url))
        raise AssertionError("t67: real network send attempted")
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    yield
    assert sends == []


def done(s):
    return [t["status"] for t in s.tasks()] == ["DONE"]


async def turn(tmp_path, monkeypatch, req, step):
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(step)
    out = await s.turn(req)
    sel = s.selected[-1]
    return s, out, sel, sel.calculation_report()


def replay_u3():
    fx = json.loads(FIX.read_text())
    args = fx["design_arguments"]
    old = {i for c in args.get("constraints") or [] for i in c.get("source_ids") or []}

    def step(messages):
        p = json.loads(messages[1]["content"])
        rid = next(x["event_id"] for x in p["sources"] if x.get("content_ref") == "request")
        raw = json.dumps(args, ensure_ascii=False)
        for o in old:
            raw = raw.replace(o, rid)
        return submit(json.loads(raw))
    return fx["request"], step


# (a) the relation check alone covers the needed verification -> pass, basis shown exactly
@pytest.mark.asyncio
async def test_a_relation_alone_sufficient_shows_relation_basis(tmp_path, monkeypatch):
    req = "총 30분 계획이 맞는지 봐 주세요. 걷기 10분, 뛰기 20분입니다."
    s, out, sel, rep = await turn(tmp_path, monkeypatch, req, submit({**BASE, "answer": "네, 합계가 총시간과 맞습니다."}))
    assert done(s)
    assert rep["overall"] == "pass" and rep["relations"] == "pass" and rep["calc_specs"] == "none"
    assert rep["answer_values"] == "none"
    r = rep["relation_results"]
    assert [(x["kind"], x["total"], x["sum"], x["status"]) for x in r] == [("unclear", "30", "30", "pass")]
    assert rep["basis"]["relation_checks"] == "completion_verdict"
    assert rep["basis"]["instruction_version"] == sel.instruction_version
    assert "calculation_pass" in sel.trace.reasons and "calc_relations_pass" in sel.trace.reasons


# (b) an essential relation is unverified -> shown unverified, completion stays blocked
@pytest.mark.asyncio
async def test_b_essential_relation_unverified_shown_and_blocked(tmp_path, monkeypatch):
    req = "연습은 45분 동안 퍼팅과 어프로치와 아이언을 합니다. 계획을 짜 주세요."
    s, out, sel, rep = await turn(tmp_path, monkeypatch, req, submit({
        **BASE, "answer": "퍼팅 20분, 어프로치 15분, 아이언 15분.", "completion_checks": ["계획을 제시했다"],
        "quantity_relations": [{"kind": "composition", "kind_span": "연습은 45분"}]}))
    assert not done(s)
    assert rep["relations"] == "unverified" and rep["overall"] == "unverified"
    assert any(x["origin"] == "structured_partial" and x["essential"] for x in rep["relation_results"])
    assert sel.unverified_relation_gaps() and not sel.completion_supported()


# (c1) REPLAY of the real t65 test-3 design: the relation passes, the answer's numbers (incl. the
# derived '여유 10분') are checked by nothing -> not shown as fully verified; completion unchanged (DONE)
@pytest.mark.asyncio
async def test_c1_replay_t65_u3_relation_pass_answer_values_not_checked(tmp_path, monkeypatch):
    req, step = replay_u3()
    s, out, sel, rep = await turn(tmp_path, monkeypatch, req, step)
    assert done(s) and "여유" in out
    # t68 (D1 fix): the answer's 15/10/10, 35<=45 and 45-35=10 are now compared in code
    # (was: answer_values "not_checked", overall "unverified" - the D1 state this test documented)
    assert rep["relations"] == "pass" and rep["answer_values"] == "verified_by_relation"
    assert rep["overall"] == "pass" and rep["calc_specs"] == "none"
    r = rep["relation_results"]
    assert [(x["kind"], x["total"], x["sum"], x["status"], x["recovered_partial"]) for x in r] == \
        [("upper_bound", "45", "35", "pass", True)]
    assert rep["basis"]["relation_checks"] == "completion_verdict"
    assert "calc_relations_pass" in sel.trace.reasons and "calc_answer_values_verified_by_relation" in sel.trace.reasons


# (c2) the relation passes but a required calculation check has no computed spec -> not fully verified
@pytest.mark.asyncio
async def test_c2_relation_pass_but_required_calc_unverified(tmp_path, monkeypatch):
    req = "총 30분으로 걷기 10분, 뛰기 20분 계획을 짜 주세요."
    s, out, sel, rep = await turn(tmp_path, monkeypatch, req, submit({
        **BASE, "answer": "걷기 10분, 뛰기 20분, 합계 30분.", "completion_checks": ["걷기 10분과 뛰기 20분의 비율 1:2 = 0.5"]}))
    assert rep["relations"] == "pass"
    assert rep["calc_specs"] == "unverified" and rep["calc_spec_missing"]
    assert rep["overall"] != "pass"
    assert not done(s)   # existing calc_spec_missing block, unchanged


# (d) a general non-calculation answer -> no needless unverified marker
@pytest.mark.asyncio
async def test_d_general_answer_no_unverified_marker(tmp_path, monkeypatch):
    s, out, sel, rep = await turn(tmp_path, monkeypatch, "TCP와 UDP 차이를 설명해 주세요.",
                                  submit({**BASE, "answer": "TCP는 연결 지향, UDP는 비연결입니다."}))
    assert done(s)
    assert rep["overall"] == "not_applicable" and rep["relations"] == "none" and rep["answer_values"] == "none"
    assert not any("unverified" in r for r in sel.trace.reasons if r.startswith("calc"))
    assert sel.calculation_status()["overall"] == "unverified"   # legacy status unchanged (t40 contract)

"""t63: a partially filled optional quantity_relations entry must not reject an otherwise valid design.

REAL-RECORD REPLAY: tests/agent/fixtures/t63/*_rejected_design.json are the three first designs the real
model sent in t62 (/workspace/t62/run, rejected there by schema validation). Only design arguments and the
user's raw text are kept (no auth data); only event ids are remapped to the replayed session's ids.
MOCK: executor answers and the constructed designs of the field-presence / counter cases.
No complete mock relation is injected in the replay or counter cases. No network, no real model call.
"""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nanobot.agent.context_selection import IntentDesign, parse_design_arguments
from .t51_harness import Session, final
from .test_context_t58_local import submit

FIX = Path(__file__).parent / "fixtures" / "t63"
ASK = "확인이 필요합니다"
NO_SEARCH = {"target": "answer", "lookup": "unneeded", "external_change": "none"}
BASE = dict(goal="계획", request_type="other", task_relation="new", task_id=None, route="direct",
            constraints=[], information_needs=[], capabilities_needed=[], completion_checks=[],
            unresolved=[], answer=None)


def fixture(name):
    return json.loads((FIX / f"{name}_rejected_design.json").read_text())


def replay_step(name):
    """The recorded arguments, with the recorded request event id replaced by this session's id."""
    args = fixture(name)["design_arguments"]
    old = {i for c in args.get("constraints") or [] for i in c.get("source_ids") or []}

    def step(messages):
        p = json.loads(messages[1]["content"])
        rid = next(x["event_id"] for x in p["sources"] if x.get("content_ref") == "request")
        raw = json.dumps(args, ensure_ascii=False)
        for o in old:
            raw = raw.replace(o, rid)
        return submit(json.loads(raw))
    return step


def raw_design(**kw):
    """MOCK design delivered as raw arguments (as the real model does), so a partial relation reaches
    the product's own validation instead of being validated in the test."""
    return submit({**BASE, **kw})


async def raw_turn(tmp_path, monkeypatch, req, **kw):
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(raw_design(**kw))
    out = await s.turn(req)
    return s, out, s.selected[-1]


def design_calls(s):
    return sum(1 for r in s.mock.requests if r["design"])


def rel(sel):
    return getattr(sel, "relation_checks", lambda: [])()


def done(s):
    return [t["status"] for t in s.tasks()] == ["DONE"]


async def replay(tmp_path, monkeypatch, name, steps=()):
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(replay_step(name))
    s.mock.steps.extend(steps)
    out = await s.turn(fixture(name)["request"])
    return s, out, s.selected[-1]


# 0. fixtures: design arguments + raw text only -------------------------------------------------------
@pytest.mark.parametrize("name", ["a1", "s2", "s3"])
def test_fixture_has_no_auth_data_and_keeps_partial_relation(name):
    f = fixture(name)
    assert set(f) == {"source", "request", "design_arguments", "t62_rejection"}
    low = json.dumps(f).lower()
    assert not any(k in low for k in ("bearer", "access_token", "refresh_token", "authorization", "cookie"))
    q = f["design_arguments"]["quantity_relations"]
    assert q and all(set(r) == {"kind", "kind_span"} for r in q)          # the real partial shape


# 4. field presence, separately ---------------------------------------------------------------------
def test_field_absent_ok():
    d = parse_design_arguments(dict(BASE))
    assert d.quantity_relations == [] and getattr(d, "quantity_relations_unverified", []) == []


def test_empty_list_ok():
    d = parse_design_arguments({**BASE, "quantity_relations": []})
    assert d.quantity_relations == [] and d.quantity_relations_unverified == []


def test_partial_relation_kept_unverified_with_reason_not_filled():
    d = parse_design_arguments({**BASE, "quantity_relations": [{"kind": "composition", "kind_span": "총 45분"}]})
    assert d.quantity_relations == []                                      # never promoted / filled
    [p] = d.quantity_relations_unverified
    assert p.kind == "composition" and p.kind_span == "총 45분"
    assert p.unit is None and p.total is None and p.items == []           # nothing invented
    assert sorted(p.missing) == ["total", "unit"]
    again = IntentDesign.model_validate(d.model_dump())                    # survives the stored proposal
    assert again.quantity_relations_unverified == d.quantity_relations_unverified


def test_complete_relation_ok():
    rel_ = {"kind": "composition", "kind_span": "총 45분", "unit": "분",
            "total": {"value": 45, "source_span": "총 45분"},
            "items": [{"label": "퍼팅", "value": 20, "source_span": "퍼팅 20분"}]}
    d = parse_design_arguments({**BASE, "quantity_relations": [rel_]})
    assert len(d.quantity_relations) == 1 and d.quantity_relations_unverified == []


@pytest.mark.parametrize("bad", [
    {"quantity_relations": "총 45분"},                                         # not a list
    {"quantity_relations": ["composition"]},                                  # entry not an object
    {"quantity_relations": [{"kind": "sum", "kind_span": "총 45분"}]},          # bad enum, partial
    {"quantity_relations": [{"kind": "composition", "unit": 5}]},             # wrong type + missing
    {"quantity_relations": [{"kind_span": "총 45분", "unit": "분"}]},            # kind itself missing
    {"quantity_relations": [{"kind": "composition", "unit": "분", "total": {"value": "45", "source_span": "x"}}]},
    {"quantity_relations": [{"kind": "composition", "kind_span": "x", "bogus": 1}]},  # unknown key
    {"quantity_relations_unverified": [{"kind": "composition"}]},             # internal field not from the model
])
def test_wrong_types_in_relation_still_reject(bad):
    with pytest.raises(ValidationError):
        parse_design_arguments({**BASE, **bad})


@pytest.mark.parametrize("bad", [
    {"route": "answer"}, {"constraints": "x"}, {"goal": 3}, {"unresolved": None}, {"task_relation": 1},
])
def test_core_field_errors_still_reject_even_with_partial_relation(bad):
    with pytest.raises(ValidationError):
        parse_design_arguments({**BASE, **bad, "quantity_relations": [{"kind": "upper_bound", "kind_span": "45분"}]})


# 5. the three real rejected designs ----------------------------------------------------------------
@pytest.mark.parametrize("name", ["a1", "s2", "s3"])
def test_real_payload_parses(name):
    d = parse_design_arguments(fixture(name)["design_arguments"])
    assert d.quantity_relations == [] and len(d.quantity_relations_unverified) == 1


@pytest.mark.asyncio
async def test_real_s2_total_conflict_asks_naming_45_and_50_no_repair(tmp_path, monkeypatch):
    s, out, sel = await replay(tmp_path, monkeypatch, "s2")
    assert design_calls(s) == 1                                            # no design repair call
    assert out.startswith(ASK) and "45분" in out and "50분" in out and not done(s)
    r = [x for x in rel(sel) if x["status"] == "conflict"]
    assert r and r[0]["origin"] == "user_text" and r[0]["total"] == "45" and r[0]["sum"] == "50"


S3_ANSWER = "45분 이내 계획: 퍼팅 15분, 어프로치 10분, 아이언 10분(합 35분). 남는 10분은 여유로 둡니다."


@pytest.mark.asyncio
async def test_real_s3_upper_bound_completes_after_check_no_repair(tmp_path, monkeypatch):
    s, out, sel = await replay(tmp_path, monkeypatch, "s3", steps=[final(S3_ANSWER)])
    assert design_calls(s) == 1
    assert out == S3_ANSWER and done(s) and not out.startswith(ASK)
    r = rel(sel)
    assert [x["status"] for x in r] == ["pass"] and r[0]["kind"] == "upper_bound" and r[0]["recovered_partial"]


@pytest.mark.asyncio
async def test_real_a1_proceeds_normally_no_repair(tmp_path, monkeypatch):
    s, out, sel = await replay(tmp_path, monkeypatch, "a1")
    answer = fixture("a1")["design_arguments"]["answer"]
    assert design_calls(s) == 1
    assert out == answer and done(s)
    r = rel(sel)
    assert [x["status"] for x in r] == ["pass"] and r[0]["kind"] == "remainder" and r[0]["recovered_partial"]


# 6. counter cases: a partial relation must not produce a wrong DONE / a needless question ----------
@pytest.mark.asyncio
async def test_partial_essential_relation_not_recoverable_does_not_complete(tmp_path, monkeypatch):
    """The user states the total in a form raw-text extraction does not cover; the model sends only
    kind/kind_span. Not recoverable -> unverified, DONE refused, no user question for the model's gap."""
    req = "연습은 45분 동안 퍼팅과 어프로치와 아이언을 합니다. 계획을 짜 주세요."
    s, out, sel = await raw_turn(tmp_path, monkeypatch, req, answer="퍼팅 20분, 어프로치 15분, 아이언 15분.",
                                 completion_checks=["계획을 제시했다"], obligations=NO_SEARCH,
                                 quantity_relations=[{"kind": "composition", "kind_span": "연습은 45분"}])
    assert not done(s) and not out.startswith(ASK)
    assert out.startswith("요청을 완료하지 못했습니다")
    r = [x for x in rel(sel) if x["origin"] == "structured_partial"]
    assert r and r[0]["status"] == "insufficient_info" and r[0]["essential"]
    assert "missing:" in r[0]["reason"] and r[0]["total"] is None          # nothing invented


@pytest.mark.asyncio
async def test_partial_relation_recovered_from_raw_text_conflict_still_asks(tmp_path, monkeypatch):
    """Counter to a wrong DONE: the partial says 'composition' and the answer looks complete, but the
    user's own numbers conflict (45 vs 50) -> the recovered raw-text check asks."""
    req = "퍼팅, 어프로치, 아이언으로 총 45분 연습 계획을 만들어 주세요. 퍼팅 20분, 어프로치 15분, 아이언 15분으로 해 주세요."
    s, out, sel = await raw_turn(tmp_path, monkeypatch, req, answer="총 45분: 퍼팅 20분, 어프로치 15분, 아이언 15분.",
                                 completion_checks=["계획을 제시했다"], obligations=NO_SEARCH,
                                 quantity_relations=[{"kind": "composition", "kind_span": "총 45분"}])
    assert out.startswith(ASK) and "50분" in out and not done(s)


@pytest.mark.asyncio
async def test_partial_relation_span_not_in_user_text_not_blocking_alone(tmp_path, monkeypatch):
    """A partial whose span is not the user's words is kept unverified but is not essential by itself."""
    req = "아침 루틴 세 가지를 번호 목록으로 알려 주세요."
    s, out, sel = await raw_turn(tmp_path, monkeypatch, req, goal="루틴", answer="1. 물 2. 스트레칭 3. 산책",
                                 completion_checks=["세 가지를 번호 목록으로 제시했다"], obligations=NO_SEARCH,
                                 quantity_relations=[{"kind": "composition", "kind_span": "총 30분"}])
    r = [x for x in rel(sel) if x["origin"] == "structured_partial"]
    assert r and r[0]["status"] == "insufficient_info" and not r[0]["essential"]
    assert out == "1. 물 2. 스트레칭 3. 산책" and done(s)

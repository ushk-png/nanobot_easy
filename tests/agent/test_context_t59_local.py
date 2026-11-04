"""t59: carried-condition provenance (defect 1) and calc-needed-without-spec (defect 2), MOCK provider.

Labels per test:
REAL-RECORD REPLAY - the t56 A1/A2 raw designs and the recorded A2 executor text (ids remapped only).
MOCK SUPPLEMENT    - an executor step the t56 record does not contain (a calculation supplement call,
                     an altered answer, a constructed design or payload for a pure-function check).
Test code never edits requirements or verification state of a running turn: the model side is the
mock queue; checks read the real SelectedContext after the turn.
"""
import copy
import json

import pytest

from nanobot.agent.context_selection import (IntentDesign, trusted_obligations,
                                             verify_carried_conditions)
from .t51_harness import Session, calls, design, final
from .test_context_t58_local import a1_step, a2_step, executor_text, record, wrong_split, wrong_total

A1_SPAN_SEARCH, A1_SPAN_DRIVER, A1_SPAN_CLUBS = "인터넷 검색은 하지 마세요.", "드라이버는 제외하고", "퍼팅, 어프로치, 아이언으로"


def calc_args(**over):
    """MOCK SUPPLEMENT: the calculation an executor states from the user's words (A2 + verified A1)."""
    c = {"unit": "분", "total": {"value": 45, "source_span": "총 45분으로 줄여 주세요."},
         "items": [{"label": "퍼팅", "value": 20, "source_span": "퍼팅은 20분이고"},
                   {"label": "어프로치", "share": "equal_remainder", "source_span": "나머지는 반씩 나눠 주세요."},
                   {"label": "아이언", "share": "equal_remainder", "source_span": "나머지는 반씩 나눠 주세요."}],
         "not_allocated": [{"label": "드라이버", "source_span": A1_SPAN_DRIVER}]}
    c.update(over)
    return {"reason": "supplement_calculation", "information_needs": [], "completion_checks": [], "calculation": c}


def supplement(args):
    return calls(("calc1", "revise_context_requirements", args))


async def run_a2(tmp_path, monkeypatch, first, after=(), max_iterations=None):
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(a1_step)
    assert await s.turn(record(1)["request"]) == record(1)["reply"]
    if max_iterations is not None:
        s.loop.max_iterations = max_iterations          # run budget (configuration), not run state
    s.mock.designs.append(a2_step())
    s.mock.steps.extend([final(first), *after])
    out = await s.turn(record(2)["request"])
    return s, out, s.selected[-1]


def tool_results(s, name="revise_context_requirements"):
    return list(dict.fromkeys(m["content"] for r in s.executor_requests() for m in r["messages"]
                              if m.get("role") == "tool" and m.get("name") == name))


def fix_messages(s):
    return list(dict.fromkeys(m["content"] for r in s.executor_requests() for m in r["messages"]
                              if m.get("role") == "user" and "계산 검증 필요·명세 없음" in str(m.get("content"))))


def carried(sel):
    return {(r["kind"], r["text"]): (r["status"], r["effect"]) for r in sel.carried_report()}


# 1 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_1_real_replay_spec_less_a2_evidence_restored_then_spec_supplemented_completes(tmp_path, monkeypatch):
    """REAL-RECORD REPLAY (design + recorded answer) + MOCK SUPPLEMENT (one calculation call)."""
    s, out, sel = await run_a2(tmp_path, monkeypatch, executor_text(),
                               [supplement(calc_args()), final(executor_text())])
    # defect 1: the kept turn-1 conditions are linked to their original event of this task
    rows = carried(sel)
    assert rows[("kept", "드라이버는 제외하고 인터넷 검색은 하지 마세요.")] == ("valid", "kept")
    assert rows[("constraint", A1_SPAN_SEARCH)] == ("valid", "kept")
    assert rows[("constraint", A1_SPAN_DRIVER)] == ("valid", "kept")
    assert rows[("constraint", A1_SPAN_CLUBS)] == ("valid", "kept")
    assert all(r["source_id"] == next(x["event_id"] for x in s.design_payloads()[-1]["sources"]
                                      if x["event_type"] == "USER_MESSAGE" and x.get("content_ref") != "request")
               for r in sel.carried_report())
    assert trusted_obligations(sel.design, sel.request_text) is not None
    assert "carried_relation_verified:lookup" in sel.relations().notes
    assert sel.relations().lookup == "forbidden"                  # the search ban still applies
    # defect 2: calc needed + no spec was recorded, the loop returned once, code compared the values
    assert sel.design.completion_specs == []                       # the first design is unchanged
    assert len(fix_messages(s)) == 1
    res = json.loads(tool_results(s)[0])
    assert res["expected_values"]["values"] == {"퍼팅": 20.0, "어프로치": 12.5, "아이언": 12.5}
    assert res["current_answer"] == "consistent"
    assert sel.calculation_status()["checks"][sel.design.completion_checks[0]] == "pass"
    assert sel.calc_spec_missing() == [] and sel.completion_supported() is True
    assert out == executor_text()
    assert [t["status"] for t in s.tasks()] == ["DONE"]
    assert len(s.mock.requests) == 5 and s.mock.steps == []        # design x2, executor x3


@pytest.mark.asyncio
async def test_1b_real_replay_without_supplement_stops_honestly(tmp_path, monkeypatch):
    """REAL-RECORD REPLAY only: the executor repeats the recorded answer and never states the calculation."""
    s, out, sel = await run_a2(tmp_path, monkeypatch, executor_text(), [final(executor_text())])
    assert trusted_obligations(sel.design, sel.request_text) is not None     # defect 1 fixed
    assert out.startswith("요청을 완료하지 못했습니다") and "계산 검증 필요·명세 없음" in out
    assert sel.calculation_status()["overall"] == "unverified"                # never pass
    assert [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]
    assert len(s.mock.requests) == 4


# 2 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_wrong_split_is_reported_by_the_supplement_and_fixed_then_completes(tmp_path, monkeypatch):
    """MOCK SUPPLEMENT: wrong split first; the supplement result shows the mismatch; corrected answer."""
    s, out, sel = await run_a2(tmp_path, monkeypatch, wrong_split(),
                               [supplement(calc_args()), final(executor_text())])
    assert json.loads(tool_results(s)[0])["current_answer"].startswith("computed_value_not_in_answer")
    assert out == executor_text() and [t["status"] for t in s.tasks()] == ["DONE"]


@pytest.mark.asyncio
async def test_2b_wrong_total_kept_is_a_mismatch(tmp_path, monkeypatch):
    s, out, sel = await run_a2(tmp_path, monkeypatch, wrong_total(),
                               [supplement(calc_args()), final(wrong_total())])
    assert sel.calculation_status()["overall"] == "mismatch"
    assert out.startswith("요청을 완료하지 못했습니다") and [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]


# 3 ------------------------------------------------------------------------------------------------
def putting_changed():
    return (executor_text().replace("퍼팅은 그대로 20분", "퍼팅은 15분").replace("퍼팅 20분", "퍼팅 15분")
            .replace("12.5", "15").replace("25분", "30분"))


@pytest.mark.asyncio
async def test_3_total_45_but_putting_not_20_fails(tmp_path, monkeypatch):
    """MOCK SUPPLEMENT: 15 + 15 + 15 = 45 (total right, putting changed)."""
    s, out, sel = await run_a2(tmp_path, monkeypatch, putting_changed(),
                               [supplement(calc_args()), final(putting_changed())])
    assert "45분" in putting_changed() and "퍼팅 15분" in putting_changed()
    assert sel.calculation_status()["overall"] == "mismatch"
    assert out.startswith("요청을 완료하지 못했습니다") and [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]


# 4 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_4_total_right_but_rest_not_halved_fails(tmp_path, monkeypatch):
    """MOCK SUPPLEMENT: 20 + 15 + 10 = 45 kept after the supplement."""
    s, out, sel = await run_a2(tmp_path, monkeypatch, wrong_split(),
                               [supplement(calc_args()), final(wrong_split())])
    assert sel.calculation_status()["overall"] == "mismatch"
    assert out.startswith("요청을 완료하지 못했습니다") and [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]


# 5 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["answer_spans", "value_not_in_span"])
async def test_5_spec_built_from_answer_values_is_rejected(tmp_path, monkeypatch, bad):
    """MOCK SUPPLEMENT: a self-validating calculation (spans/values taken from the answer)."""
    if bad == "answer_spans":
        args = calc_args(total={"value": 45, "source_span": "총 45분으로 줄인 연습 계획입니다."},
                         items=[{"label": "어프로치", "value": 12.5, "source_span": "어프로치 12.5분"}])
    else:
        args = calc_args(items=[{"label": "퍼팅", "value": 20, "source_span": "퍼팅은 20분이고"},
                                {"label": "어프로치", "value": 12.5, "source_span": "나머지는 반씩 나눠 주세요."},
                                {"label": "아이언", "value": 12.5, "source_span": "나머지는 반씩 나눠 주세요."}])
    s, out, sel = await run_a2(tmp_path, monkeypatch, executor_text(), [supplement(args), final(executor_text())])
    err = tool_results(s)[0]
    assert "calculation rejected" in err
    assert ("spec_from_answer_values" in err) if bad == "answer_spans" else ("value_not_in_user_span" in err)
    assert sel.calc_supplements == {} and sel.calc_spec_missing()
    assert out.startswith("요청을 완료하지 못했습니다") and "계산 검증 필요·명세 없음" in out


# 6 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_6_prior_condition_explicitly_changed_in_followup_uses_the_change(tmp_path, monkeypatch):
    """MOCK SUPPLEMENT (constructed follow-up design): the design still carries the turn-1 search ban,
    but the follow-up asks to search: the newer instruction supersedes the carried prohibition."""
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(a1_step)
    await s.turn(record(1)["request"])
    req = "이번에는 인터넷 검색을 해서 퍼팅 연습법을 찾아 주세요."

    def step(messages):
        p = json.loads(messages[1]["content"])
        a1 = next(x["event_id"] for x in p["sources"] if x["event_type"] == "USER_MESSAGE" and x.get("content_ref") != "request")
        task = p["prior_context"]["current_task_id"]
        d = design(goal="퍼팅 연습법 검색", task_relation="change", task_id=task, route="execute",
                   capabilities_needed=["web_search"], completion_checks=["검색 결과를 출처와 함께 제시"],
                   constraints=[{"text": A1_SPAN_SEARCH, "source_ids": [a1]}],
                   obligations={"target": "answer", "lookup": "required", "external_change": "none",
                                "source_span": "인터넷 검색을 해서",
                                "relations": [{"action": "lookup", "target": "web", "necessity": "forbidden",
                                               "source_span": A1_SPAN_SEARCH}]})
        from .t51_harness import design_response
        return design_response(d)
    s.mock.designs.append(step)
    s.mock.steps.extend([final("검색을 실행하지 못했습니다.")] * 3)
    await s.turn(req)
    sel = s.selected[-1]
    rel = next(r for r in sel.carried_report() if r["kind"] == "relation")
    assert (rel["status"], rel["reason"], rel["effect"]) == ("superseded", "changed_by_current_request",
                                                              "replaced_by_current_request")
    assert "carried_relation_superseded:lookup" in sel.relations().notes
    assert sel.relations().lookup == "required"
    assert sel.lookup_gate("web_search") is None             # the changed condition applies


# 7 ------------------------------------------------------------------------------------------------
@pytest.fixture
def a2_inputs(tmp_path, monkeypatch):
    """REAL-RECORD inputs for a pure-function check: the A2 design and the real A2 payload."""
    async def build():
        s = Session(tmp_path, monkeypatch)
        s.mock.designs.append(a1_step)
        await s.turn(record(1)["request"])
        captured = {}
        inner = a2_step()

        def step(messages):
            out = inner(messages)
            captured["args"] = out.tool_calls[0].arguments
            return out
        s.mock.designs.append(step)
        s.mock.steps.extend([final(executor_text())] * 2)
        await s.turn(record(2)["request"])
        wire = s.design_payloads()[-1]
        # the validator's input: the wire payload with the request source's content restored
        sources = [{**x, "content": wire["request"]} if x.get("content_ref") == "request" else x
                   for x in wire["sources"]]
        payload = {"request": wire["request"], "sources": sources, "task_state": wire["task_state"],
                   "prior_context": wire["prior_context"]}
        return captured["args"], payload
    return build


def _src(payload):
    return next(x["event_id"] for x in payload["sources"]
                if x.get("event_type") == "USER_MESSAGE" and x.get("content") != payload["request"])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["nonexistent_source", "other_task_source", "other_task_link"])
async def test_7_wrong_carry_over_is_rejected(a2_inputs, case):
    """MOCK SUPPLEMENT (constructed design/payload variants of the real A2 inputs)."""
    args, payload = await a2_inputs()
    args, payload = copy.deepcopy(args), copy.deepcopy(payload)
    a1 = _src(payload)
    ban = "외부 자료는 보지 마세요."                 # a prohibition that is not in this task's records
    args["obligations"]["source_span"] = ban
    args["obligations"]["relations"][-1]["source_span"] = ban
    if case == "nonexistent_source":
        args = json.loads(json.dumps(args).replace(a1, "E00000000000000000000000000000000"))
    elif case == "other_task_source":
        payload["sources"].append({"event_id": "Eother0000000000000000000000000000", "event_type": "USER_MESSAGE",
                                   "actor": "user", "content": "다른 작업입니다. " + A1_SPAN_DRIVER + " " + ban})
        args = json.loads(json.dumps(args).replace(a1, "Eother0000000000000000000000000000"))
    else:
        args["task_id"] = "another-task"
        args["reference"]["task_id"] = "another-task"
    d = IntentDesign.model_validate(args)
    rows = verify_carried_conditions(d, payload)
    expect = {"nonexistent_source": "unknown_source", "other_task_source": "not_a_condition_of_the_linked_task",
              "other_task_link": "followup_not_linked_to_task"}[case]
    cited = [r for r in rows if r["kind"] in {"kept", "constraint"}]
    assert cited and all(r["status"] == "insufficient_evidence" and r["reason"] == expect for r in cited)
    banned = [r for r in rows if r["span"] == ban]
    assert banned and all(r["status"] == "insufficient_evidence" for r in banned)
    if case == "other_task_source":
        assert all(r["reason"] == expect for r in banned)   # found, but in another task's event
    assert trusted_obligations(d, payload["request"]) is None
    # the unverified prohibition is never deleted: it stays a restriction
    from nanobot.agent.context_selection import effective_relations
    eff = effective_relations(d, payload["request"], payload["sources"], [])
    assert eff.lookup == "forbidden" and "carried_prohibition_kept_unverified:lookup" in eff.notes


# 8 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_8_polite_rewrite_without_calculation_completes_without_calc_checks(tmp_path, monkeypatch):
    """MOCK SUPPLEMENT (constructed design): numbers in the request and answer are the user's own."""
    s = Session(tmp_path, monkeypatch)
    req = "다음 문장을 정중하게 바꿔 주세요: 회의는 3시에 2층에서 한다."
    ans = "회의는 3시에 2층에서 진행될 예정이오니 참고해 주시기 바랍니다."
    out = await s.turn(req, design(goal="정중한 문장", route="direct", answer=ans,
                                   completion_checks=["정중한 어조로 바꿨다", "3시와 2층이 그대로 유지된다"],
                                   obligations={"target": "answer", "lookup": "unneeded", "external_change": "none"}))
    sel = s.selected[-1]
    assert out == ans and sel.calc_spec_missing() == []
    assert [t["status"] for t in s.tasks()] == ["DONE"] and len(s.mock.requests) == 1


# 9 ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_9_budget_exhausted_no_extra_send_honest_stop(tmp_path, monkeypatch):
    """REAL-RECORD REPLAY with a 1-iteration executor budget: no fix call is made."""
    s, out, sel = await run_a2(tmp_path, monkeypatch, executor_text(), max_iterations=1)
    assert fix_messages(s) == [] and len(s.mock.requests) == 3 and s.mock.steps == []
    assert out.startswith("요청을 완료하지 못했습니다") and "계산 검증 필요·명세 없음" in out
    assert executor_text() in out                                    # the answer is shown as unverified
    assert sel.calculation_status()["overall"] == "unverified"
    assert [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]

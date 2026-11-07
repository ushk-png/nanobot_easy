"""t35: regressions built from the real t29 model outputs (live design sends recorded at b2ba3d2)
and the t30 multi send. Each test names the t29 turn whose failure path it pins. No network."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.context_selection import (IntentDesign, SelectedContext, argument_encoding_issues,
                                             computed_mismatch, effective_relations, instruction_entries,
                                             select_design_output)
from nanobot.agent.context_control import goal_conflict_in_scope, instruction_scope
from nanobot.agent.context_evidence import (failure_class, fingerprint, match_need, observation,
                                            text_expected_mismatch)
from nanobot.agent.context_items import need_items, requested_items
from nanobot.agent.context_trace import ContextTrace
from nanobot.providers.base import LLMResponse, ToolCallRequest

FIX = json.loads((Path(__file__).parent / "fixtures" / "t29_design_outputs.json").read_text())
REQ = FIX["requests"]


def case(label):
    c = FIX["cases"][label]
    return c, REQ[c["request"]]


def response(label):
    c, _ = case(label)
    return LLMResponse(content=c["content"], tool_calls=[
        ToolCallRequest(id=str(i), name=t["name"], arguments=t["arguments"]) for i, t in enumerate(c["tool_calls"])])


def args(label):
    return case(label)[0]["tool_calls"][0]["arguments"]


def selected(label, answer=None, ws=None, design=None):
    a = design or args(label)
    sel = SelectedContext(trace=ContextTrace("t35"), design=IntentDesign.model_validate(a))
    sel.current_event = SimpleNamespace(content=case(label)[1], sequence=5, event_id="u5")
    sel.recent, sel.artifacts = [], []
    sel.scope = SimpleNamespace(workspace_ids=[ws or "/ws"])
    sel.answer_text = a.get("answer") if answer is None else answer
    return sel


# ---- 1/2. output selection: the single function call is the result; text never vetoes ----

TEXT_DIFFERS = ["p2-golf-r1-t2", "p2-corrected-r1-t1", "p1-q2-A-r3-t1", "p1-q2-B-r1-t1",
                "p1-q2-B-r2-t1", "p1-q2-B-r3-t1", "t30-p2-multi-r1-t1"]


@pytest.mark.parametrize("label", sorted(FIX["cases"]))
def test_real_single_call_is_the_design(label):
    got, status, errors = select_design_output(response(label))
    assert errors == [] and got == args(label)
    assert status in {"identical_text_duplicate_ignored", "text_design_differs_ignored"}


@pytest.mark.parametrize("label", TEXT_DIFFERS)
def test_text_wording_differences_are_recorded_not_repaired(label):
    # b2ba3d2 sent these to repair (golf t2/corrected: text "똑같이" vs args "같이"; t30: escapes).
    assert select_design_output(response(label))[1] == "text_design_differs_ignored"


def test_differing_multiple_calls_stay_a_conflict():
    r = LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="0", name="submit_context_design", arguments=args("p2-golf-r1-t1")),
        ToolCallRequest(id="1", name="submit_context_design", arguments=args("p2-golf-r1-t2"))])
    assert select_design_output(r)[2] == ["design_conflicting_outputs"]


def test_double_escaped_argument_is_reported_not_decoded():
    issues = argument_encoding_issues(args("t30-p2-multi-r1-t1"), {"request": REQ["MULTI"]})
    assert "answer" in issues
    for label in FIX["cases"]:
        if not label.startswith("t30"):
            assert argument_encoding_issues(args(label), {"request": case(label)[1]}) == []
    # a user who really typed an escape is not an encoding error
    assert argument_encoding_issues({"answer": "\\uAC00"}, {"request": "\\uAC00를 설명해 줘"}) == []


# ---- 5. completion by result type: computed checks against the real answers ----

@pytest.mark.parametrize("label", ["p1-q1-A-r1-t1", "p1-q1-C-r1-t1", "p1-q2-B-r2-t1",
                                   "p1-q2-C-r1-t1", "p2-golf-r1-t2", "p2-corrected-r1-t1"])
def test_real_computed_answers_pass(label):
    assert selected(label).answer_check_failures() == []


def test_real_inconsistent_spec_still_fails():
    # p1-q1-B: the model declared {감소량 15000, 감소율 62.5} with total 24000 – the spec is wrong.
    assert selected("p1-q1-B-r1-t1").answer_check_failures() != []


def test_wrong_numbers_in_real_answer_fail():
    a = args("p2-golf-r1-t2")["answer"]
    assert selected("p2-golf-r1-t2", a.replace("12.5분", "15분")).answer_check_failures() != []
    rent = args("p2-rent-r1-t1")["answer"]
    assert selected("p2-rent-r1-t1", rent.replace("8,000", "9,000")).answer_check_failures() != []


def test_real_rent_spec_without_declared_structure_is_not_passed():
    # t36: the recorded rent spec lists 1인당 8000 as a part of total 48000 and declares no other
    # structure; the count of values no longer turns the sum check off, so code refuses it.
    assert selected("p2-rent-r1-t1").answer_check_failures() != []
    a = json.loads(json.dumps(args("p2-rent-r1-t1")))
    for spec in a["completion_specs"]:
        if spec.get("result_kind") == "computed":
            spec["expected_values"]["sum_parts"] = []      # share: declared "no sum"
    assert selected("p2-rent-r1-t1", design=a).answer_check_failures() == []


def test_thousands_and_exclusion():
    share = {"values": {"1인당": 8000}, "total": 48000, "sum_parts": []}
    assert computed_mismatch(share, "48,000원 ÷ 6명\n1인당 8,000원") is None
    e = {"values": {"퍼팅": 20, "아이언": 25}, "total": 45, "excluded": ["드라이버"], "not_allocated": ["드라이버"]}
    assert computed_mismatch(e, "퍼팅 20분\n아이언 25분\n합계 45\n드라이버 10분") == "computed_excluded_item_allocated:드라이버"
    assert computed_mismatch(e, "퍼팅 20분, 아이언 25분, 드라이버: 10분, 합계 45") == "computed_excluded_item_allocated:드라이버"
    assert computed_mismatch(e, "총 45분 (드라이버 제외)\n퍼팅 20분\n아이언 25분") is None
    assert computed_mismatch(e, "퍼팅 20 + 아이언 25 = 45. 드라이버 0.") is None


# ---- 5. prohibition compliance from execution records (perm t1, museum) ----

@pytest.mark.parametrize("label,check", [
    ("p2-perm-r1-t1", "notes.md를 포함한 어떤 파일도 수정·저장하지 않는다."),
    ("p2-museum-r1-t1", "예약, 결제, 파일 저장은 수행하지 않는다.")])
def test_prohibition_check_is_verified_from_executions(label, check):
    sel = selected(label)
    assert sel.compliance_check(check)
    assert sel.compliance_failures() == []
    sel.observations["w"] = {"tool_call_id": "w", "name": "write_file", "status": "ok", "read_only": False}
    assert sel.compliance_failures() == [check]
    sel.observations["w"]["status"] = "error"
    assert sel.compliance_failures() == []


def test_change_request_check_is_not_compliance():
    sel = selected("p2-file-r1-t1")
    assert not any(sel.compliance_check(c) for c in sel.design.completion_checks)


# ---- 4. j2 (0037) regression: coordinated clause grounded by its sibling ----

def rels(label):
    return args(label)["obligations"]["relations"]


def eff_for(label, request, relations):
    a = dict(args(label)); a["obligations"] = {**a["obligations"], "relations": relations}
    return effective_relations(IntentDesign.model_validate(a), request, [], [])


def test_file_t1_sibling_clause_is_grounded():
    eff = eff_for("p2-file-r1-t1", REQ["FILE1"], rels("p2-file-r1-t1"))
    assert not [c for c in eff.conflicts if c["code"] == "model_target_ungrounded"]
    assert "model_target_grounded_by_sibling_clause:write" in eff.notes


@pytest.mark.parametrize("mutate", ["other_target", "sibling_is_inspect", "no_sibling"])
def test_ungrounded_without_a_real_sibling(mutate):
    r = [dict(x) for x in rels("p2-file-r1-t1")]
    request = REQ["FILE1"]
    if mutate == "other_target":
        r[0]["target_ref"] = "/elsewhere/other.md"
    elif mutate == "sibling_is_inspect":
        r[0]["action"] = "inspect"
    else:
        r = [r[1]]
    eff = eff_for("p2-file-r1-t1", request, r)
    assert [c for c in eff.conflicts if c["code"] == "model_target_ungrounded"]


# ---- 3. information needs by stage; directory evidence (file t1 exam-folder) ----

def test_directory_need_is_met_by_listing(tmp_path):
    need = IntentDesign.model_validate(args("p2-file-r1-t1")).information_needs[0].model_copy(
        update={"target": str(tmp_path), "query": str(tmp_path)})
    sel = selected("p2-file-r1-t1", ws=str(tmp_path))
    call = SimpleNamespace(id="l1", name="list_dir", arguments={"path": str(tmp_path)})
    tool = SimpleNamespace(read_only=True)
    rec = observation(sel, call, "USER.md", "ok", tool)
    assert rec["result_kind"] == "directory_listing"
    assert match_need(need, [rec], str(tmp_path))[0]
    (tmp_path / "notes.md").write_text("회의 시간: 10시\n")       # creating the file keeps the folder identity
    assert match_need(need, [rec], str(tmp_path))[0]
    read = {**rec, "result_kind": "file_content", "name": "read_file"}
    assert not match_need(need, [read], str(tmp_path))[0]
    assert fingerprint(tmp_path) == rec["version"]


def test_new_file_need_does_not_gate_its_own_creation(tmp_path):
    a = json.loads(json.dumps(args("p2-file-r1-t1")).replace("/workspace/t29/runs/ws/p2-file-r1", str(tmp_path)))
    target = str(tmp_path / "notes.md")
    a["information_needs"] = [{**a["information_needs"][0], "id": "notes-content", "target": target,
                               "query": target}]
    sel = SelectedContext(trace=ContextTrace("t35"), design=IntentDesign.model_validate(a))
    sel.current_event = SimpleNamespace(content=REQ["FILE1"], sequence=5, event_id="u5")
    sel.recent, sel.artifacts = [], []
    sel.scope = SimpleNamespace(workspace_ids=[str(tmp_path)])
    sel.missing = ["notes-content"]
    assert sel.need_stage(sel.design.information_needs[0]) == "after_change"
    assert sel.evidence_error(for_mutation=True) is None          # creation is allowed
    assert sel.evidence_error() is not None                        # completion still needs it
    # t36: "expected new" is the pre-change snapshot; a file that appears afterwards is not read
    # as a reason to require a pre-read, it is a creation conflict (never overwritten).
    (tmp_path / "notes.md").write_text("x")
    assert sel.creation_conflict("write_file", {"path": target, "content": "y"})


def test_text_file_expected_values(tmp_path):
    f = tmp_path / "notes.md"; f.write_text("회의 시간: 10시\n")
    t = str(f)
    assert text_expected_mismatch(t, {"content": "회의 시간: 10시"}, str(tmp_path)) is None
    assert text_expected_mismatch(t, {"path": "notes.md"}, str(tmp_path)) is None
    assert text_expected_mismatch(t, {"contains": "10시"}, str(tmp_path)) is None
    assert text_expected_mismatch(t, {"content": "회의 시간: 11시"}, str(tmp_path))
    assert text_expected_mismatch(t, {"path": "other.md"}, str(tmp_path))
    assert "not recognized" in text_expected_mismatch(t, {"mutated": False}, str(tmp_path))


# ---- 6. one current instruction state: perm t2 goal vs the earlier prohibition ----

def payload(request, earlier):
    return {"request": request, "sources": [{"event_id": "u1", "sequence": 1, "actor": "user"}],
            "task_state": {"valid_constraint_candidates": [{"source_id": "u1", "text": earlier,
                                                             "status": "user_original"}]}}


def test_perm_t2_goal_is_allowed_after_the_grant():
    goal = args("p2-perm-r1-t2")["goal"]
    assert not goal_conflict_in_scope(goal, instruction_scope(instruction_entries(payload(REQ["PERM2"], REQ["PERM1"]))))


@pytest.mark.parametrize("request_text", ["확인했습니다.", "확인했습니다. 이제 그 변경안을 other.md에 적용해 주세요."])
def test_denial_stands_without_a_grant_for_that_target(request_text):
    goal = args("p2-perm-r1-t2")["goal"]
    assert goal_conflict_in_scope(goal, instruction_scope(instruction_entries(payload(request_text, REQ["PERM1"]))))


def test_later_denial_wins_over_earlier_grant():
    p = payload(REQ["PERM1"], REQ["PERM2"])   # grant first, then "지금은 파일을 수정하지 마세요"
    assert goal_conflict_in_scope(args("p2-perm-r1-t2")["goal"], instruction_scope(instruction_entries(p)))


# ---- 7. museum: items per need, failure classes ----

def test_museum_items_are_split_per_need():
    needs = IntentDesign.model_validate(args("p2-museum-r1-t1")).information_needs
    assert set(requested_items(REQ["MUSEUM"])) == {"hours", "fee", "exhibition_kind", "official_link", "check_date"}
    by = {n.id: need_items(REQ["MUSEUM"], n, needs) for n in needs}
    assert set(by["permanent_hours_fee"]) == {"hours", "fee", "exhibition_kind", "official_link", "check_date"}
    assert set(by["special_vs_permanent"]) == {"exhibition_kind", "official_link", "check_date"}


def test_museum_failure_classes():
    tls = ("Error: DuckDuckGo search failed (RequestError: RequestError('error sending request for url "
           "(https://html.duckduckgo.com/html/) > tls handshake eof'))")
    assert failure_class("web_search", tls, "ok") == "transport"
    assert failure_class("web_search", "Error: DuckDuckGo search failed (No results found.)", "ok") == "no_results"
    assert failure_class("web_fetch", '{"error": "", "url": "https://www.museum.go.kr/site/main/home"}', "ok") \
        == "fetch_rejected"
    assert failure_class("web_fetch", '{"url": "https://x", "status": 200, "text": "page"}', "ok") is None

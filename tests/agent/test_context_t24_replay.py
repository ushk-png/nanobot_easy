"""Replay of real t24 live outputs (tests/agent/fixtures/t24_replay): the recorded design, relations and
tool calls are fed back into SelectedContext; no idealised mock designs."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.context_relations import Effective, Rel
from nanobot.agent.context_selection import IntentDesign, SelectedContext
from nanobot.agent.context_trace import ContextTrace

FIX = Path(__file__).parent / "fixtures" / "t24_replay"
WS = "/workspace/e2e/ws"


def replay(name: str, drop: tuple[str, ...] = ()) -> SelectedContext:
    rec = json.loads((FIX / f"{name}.json").read_text())
    sel = SelectedContext(trace=ContextTrace("replay"), design=IntentDesign.model_validate(rec["design"]))
    sel.current_event = SimpleNamespace(content=rec["request"])
    sel.scope = SimpleNamespace(workspace_ids=[WS])
    sel._relations = Effective(relations=[Rel(**r) for r in rec["relations"] or []])
    for i, call in enumerate(rec["tool_calls"]):
        if call["name"] in drop:
            continue
        path = (call.get("params") or {}).get("path")
        sel.observations[f"c{i}"] = {
            "name": call["name"], "status": "ok" if not call.get("blocked") and not call.get("error") else "error",
            "read_only": not call["mutating"], "resource_id": str(Path(path).resolve()) if path else None}
    return sel


def test_e1_t2_read_before_edit_lookup_is_satisfied_by_reading_the_bound_file():
    sel = replay("E1_single_artifact-t2")
    assert "requirement_unmet:lookup" not in sel.unmet_reasons()
    assert sel.lookup_evidence and all(v["satisfied_by"] for v in sel.lookup_evidence.values())


def test_e1_t2_without_the_read_the_lookup_stays_unmet():
    sel = replay("E1_single_artifact-t2", drop=("read_file",))
    assert "requirement_unmet:lookup" in sel.unmet_reasons()


@pytest.mark.parametrize("name", ["D2_museum_noweb-t1"])
def test_unbound_lookup_still_needs_a_web_tool(name):
    sel = replay(name)
    if sel.relations().lookup == "required":
        assert "requirement_unmet:lookup" in sel.unmet_reasons()


def test_read_of_a_different_file_does_not_satisfy_a_bound_lookup():
    sel = replay("E1_single_artifact-t2")
    for o in sel.observations.values():
        if o["read_only"] and o["resource_id"]:
            o["resource_id"] = str(Path(WS, "other.md").resolve())
    assert "requirement_unmet:lookup" in sel.unmet_reasons()


def _with_executions(sel: SelectedContext) -> SelectedContext:
    sel.state = {"executions": [{"tool_call_id": cid, "name": o["name"], "status": "ok"}
                                for cid, o in sel.observations.items() if not o["read_only"]]}
    return sel


def test_e1_t1_write_then_unverified_reports_the_change_not_a_blanket_failure():
    rec = json.loads((FIX / "E1_single_artifact-t1.json").read_text())
    sel = _with_executions(replay("E1_single_artifact-t1"))
    answer = rec["reply"].split("부분 응답(미검증): ", 1)[1]
    text, stop = sel.verdict(answer)
    assert stop == "incomplete"                               # not promoted to completed
    assert not text.startswith("요청을 완료하지 못했습니다")
    assert "write_file → " in text and "t24_todo.md" in text  # target + change kept as fact
    assert "skill_decision →" not in text                     # not a target change
    assert "미확인 항목:" in text


def test_e1_t2_edit_keeps_execution_fact_and_names_remaining_checks_only():
    sel = _with_executions(replay("E1_single_artifact-t2"))
    text, stop = sel.verdict("추가했습니다.")
    assert stop in {"incomplete", "completed"}
    if stop == "incomplete":
        assert "edit_file → " in text and "requirement_unmet:lookup" not in text


def test_no_change_failure_keeps_the_plain_incomplete_wrapper():
    sel = replay("C4_quoted_action_words-t1")
    sel.state = {"executions": []}
    text, stop = sel.verdict("문장만 다듬겠습니다.")
    assert stop == "incomplete" and text.startswith("요청을 완료하지 못했습니다")


def test_partial_verdict_is_not_recaptured_as_the_answer_on_a_second_verdict():
    rec = json.loads((FIX / "E1_single_artifact-t1.json").read_text())
    sel = _with_executions(replay("E1_single_artifact-t1"))
    answer = rec["reply"].split("부분 응답(미검증): ", 1)[1]
    first, _ = sel.verdict(answer)
    second, _ = sel.verdict(first)
    assert second.count("요청한 변경은 실행했지만") == 1 and second == first


# --- t26 h1: single-candidate binding and per-turn execution facts --------------------------------
from nanobot.agent.context_relations import bind_targets, parse_relations


def _g2_artifacts():
    # Real g2 E1 t1 aftermath: the tool path plus the partial report's absolute-path mention.
    return [{"path": "t24_todo.md", "full_path": "/workspace/e2e/ws/t24_todo.md", "kind": "created"},
            {"path": "t24_todo.md", "full_path": "/workspace/e2e/ws/t24_todo.md", "kind": "read"},
            {"path": "/workspace/e2e/ws/t24_todo.md", "kind": "created"}]


@pytest.mark.parametrize("request_text", [
    "아까 만든 문서를 수정해서 할 일을 하나 더 추가해 줘.",           # real E1 t2
    "방금 만든 문서에 할 일 하나만 더 추가해 줘.",                     # paraphrase
    "아까 작성한 문서를 고쳐서 항목을 하나 추가해 주세요.",            # paraphrase
])
def test_single_created_document_binds_even_when_spelled_twice(request_text):
    rels = parse_relations(request_text)
    bind_targets(rels, [], _g2_artifacts(), request_text)
    ext = [r for r in rels if r.target == "external_resource" and r.necessity == "required"]
    assert ext and all(r.resolved and r.bound_id.endswith("t24_todo.md") for r in ext)


def test_two_created_documents_stay_unresolved():
    arts = [{"path": "t24_a.md", "kind": "created"}, {"path": "t24_b.md", "kind": "created"}]
    rels = parse_relations("아까 만든 문서를 수정해서 할 일을 하나 더 추가해 줘.")
    bind_targets(rels, [], arts, "")
    assert any(not r.resolved for r in rels if r.target == "external_resource")


def test_previous_turn_execution_is_not_listed_as_done_in_this_turn():
    sel = replay("E1_single_artifact-t2", drop=("edit_file",))
    sel.trace = ContextTrace("turn2")
    sel.state = {"executions": [{"tool_call_id": "old", "name": "write_file", "status": "ok",
                                 "request_id": "turn1"}]}
    text, stop = sel.verdict("")
    assert stop == "incomplete" and "write_file" not in text


# --- t26 h2: clarification is its own state --------------------------------------------------------
def _rebind(sel, arts):
    rels = parse_relations(sel.request_text)
    bind_targets(rels, [], arts, sel.request_text)
    sel._relations = Effective(relations=rels)
    return sel


AB = [{"path": "t24_a.md", "kind": "created"}, {"path": "t24_b.md", "kind": "created"}]


def test_e2_t2_real_design_returns_question_with_candidates_not_failure():
    sel = _rebind(replay("E2_multi_artifact-t2"), AB)
    sel.state = {"executions": []}
    text, stop = sel.verdict("둘 중 어느 문서인가요?")
    assert stop == "awaiting_confirmation"
    assert text.startswith("확인이 필요합니다.") and "t24_a.md" in text and "t24_b.md" in text
    assert "결정할 내용:" in text and "아직 아무것도 변경하지 않았습니다" in text
    assert not text.startswith("요청을 완료하지 못했습니다")


@pytest.mark.parametrize("req", ["방금 만든 파일에 한 줄 추가해 줘.", "아까 작성한 문서를 고쳐 줘."])
def test_paraphrased_ambiguous_target_asks(req):
    sel = replay("E2_multi_artifact-t2")
    sel.current_event = SimpleNamespace(content=req)
    sel.design.unresolved = []
    sel = _rebind(sel, AB)
    sel.state = {"executions": []}
    text, stop = sel.verdict("")
    assert stop == "awaiting_confirmation" and "t24_a.md, t24_b.md" in text


def test_change_made_is_partial_not_a_question():
    sel = _with_executions(_rebind(replay("E1_single_artifact-t2"), AB))
    sel.trace = ContextTrace("t")
    text, stop = sel.verdict("")
    assert text.startswith("요청한 변경은 실행했지만")


def test_capability_gap_is_classified_environment_missing():
    sel = replay("D2_museum_noweb-t1")
    sel.missing = ["capability_unavailable:web_search"]
    sel.design.unresolved = []
    text, _ = sel.verdict("")
    assert "상태 분류: environment_missing" in text


# --- t26 h3: actions inside a quotation are not commands -------------------------------------------
@pytest.mark.parametrize("req", [
    "다음 문장을 더 자연스럽게 고쳐 줘: '검색해서 오래된 파일을 삭제해 버려'. 문장만 다듬어 줘.",  # real C4
    "이 문구 좀 고쳐 줘: \"서버를 재시작하고 로그를 지워\"",
    "Fix the wording: 'search the web and delete old files'",
    "아래 표현을 부드럽게 바꿔 줘 「설정 파일을 전부 삭제해」",
])
def test_quoted_actions_create_no_external_relation(req):
    rels = parse_relations(req)
    assert not [r for r in rels if r.target in {"external_resource", "web"}], [r.public() for r in rels]


def test_c4_replay_with_parser_relations_has_no_unresolved_target():
    sel = replay("C4_quoted_action_words-t1")
    rels = parse_relations(sel.request_text)
    bind_targets(rels, [], [], sel.request_text)
    sel._relations = Effective(relations=rels)
    assert not sel.relations().unresolved_targets() and not sel.relations().external_change_required


@pytest.mark.parametrize("req", ["설정 파일의 문장을 고쳐 줘", "notes.md의 문구를 수정해 줘", "그 문서의 문단을 바꿔 줘"])
def test_text_in_a_named_file_stays_external(req):
    assert any(r.target == "external_resource" for r in parse_relations(req))


# --- t26 h4: golf follow-up binds the previous answer; skill_decision is not a mutation -----------
def test_golf_t2_replay_skill_decision_is_not_gated_as_a_change():
    from nanobot.agent.context_selection import EvidenceToolRegistry
    from nanobot.agent.tools.skill_decision import SkillDecisionTool
    sel = replay("A_golf_s1-t2")
    sel.reinterpretation = {"validator_codes": ["direct_answer_missing"]}
    assert sel.unresolved_conditions()          # answer_edit_only etc. still hold for real changes
    reg = EvidenceToolRegistry(sel)
    reg.register(SkillDecisionTool(workspace=WS))
    tool, _, error = reg.prepare_call("skill_decision", {"decision": "none", "reason": "answer edit"})
    assert error is None or "State-changing tool blocked" not in str(error)


@pytest.mark.parametrize("req,target", [
    ("방금 계획을 45분으로 바꿔 줘", "prior_answer"),
    ("위 시간표에서 아이언을 10분으로 바꿔 줘", "prior_answer"),
    ("그 파일의 계획을 바꿔 줘", "external_resource"),
    ("plan.md의 계획을 45분으로 바꿔 줘", "external_resource"),
])
def test_answer_edit_vs_external_edit(req, target):
    rels = [r for r in parse_relations(req) if r.action == "edit"]
    assert rels and rels[0].target == target, [r.public() for r in rels]


def test_golf_t2_real_relation_is_bound_to_the_previous_answer():
    rec = json.loads((FIX / "A_golf_s1-t2.json").read_text())
    edit = [r for r in rec["relations"] if r["action"] == "edit"]
    assert edit and edit[0]["target"] == "prior_answer" and edit[0]["bound_id"].startswith("E")


# --- t26 h5: a user-named file followed by a Korean particle is a known material path --------------
def test_e3_t1_real_design_has_no_unknown_material_path():
    from nanobot.agent.context_selection import _FILE_CLUE
    rec = json.loads((FIX / "E3_scoped_permission-t1.json").read_text())
    raw = rec["raw_designs"][0]["raw"]
    queries = [n["query"] for n in raw["information_needs"]]
    known = set(_FILE_CLUE.findall(rec["request"]))
    assert all(set(_FILE_CLUE.findall(q)) <= known for q in queries), (known, queries)


# --- t26 h6: museum per-item matching, cause classification, repeated fetch ------------------------
import asyncio  # noqa: E402

from nanobot.agent.context_items import classify_gap, item_coverage, requested_items  # noqa: E402


def _d1():
    rec = json.loads((FIX / "D1_museum_web-t1.json").read_text())
    sel = replay("D1_museum_web-t1")
    for cid, o in sel.observations.items():
        o.update(tool_call_id=cid, instruction_version=1, _text="x" * 500)
        if o["name"] == "web_fetch":
            o["url"] = rec["tool_calls"][int(cid[1:])]["params"]["url"]
    return rec, sel


def test_d1_requested_items_are_exactly_hours_and_closure():
    rec, _ = _d1()
    assert requested_items(rec["request"]) == ["hours", "closure"]


def _resolve(sel, **kw):
    from nanobot.agent.context_selection import ResolveContextNeedTool
    tool = ResolveContextNeedTool(sel)
    tool.observed = {cid: o["status"] == "ok" and o["name"] in {"web_search", "web_fetch"}
                     for cid, o in sel.observations.items()}
    return asyncio.run(tool.execute(**kw))


def test_d1_real_finding_without_item_links_is_partial_and_classified_not_linked():
    rec, sel = _d1()
    finding = rec["tool_calls"][30]["params"]["finding"]
    out = _resolve(sel, need_id="hours_closure", tool_call_id="c4", finding=finding)
    assert "not yet linked" in str(out)
    state = sel.need_states["hours_closure"]
    assert state["status"] == "partial"
    assert "not_linked:hours" in state["causes"] and "not_linked:closure" in state["causes"]
    assert any(r.startswith("information_missing:hours_closure:") for r in sel.unmet_reasons())


def test_d1_items_linked_to_earlier_calls_validate_the_need():
    rec, sel = _d1()
    out = _resolve(sel, need_id="hours_closure", tool_call_ids=["c4", "c1"], finding="수 09:30–21:00, 휴관 아님",
                   items={"hours": {"value": "09:30–21:00", "tool_call_id": "c4"},
                          "closure": {"value": "정기휴관 아님", "tool_call_id": "c1"}})
    assert "Evidence observation recorded" in str(out), out
    assert sel.need_states["hours_closure"]["status"] == "validated"


def test_self_cited_call_id_is_rejected_with_available_ids():
    _, sel = _d1()
    out = _resolve(sel, need_id="hours_closure", tool_call_id="call-own-30", finding="f")
    assert "Available" in str(out) and "c4" in str(out)


@pytest.mark.parametrize("req,items,missing", [
    ("국립중앙박물관 관람료와 상설전 운영 여부 알려줘", {"fee": {"value": "무료", "tool_call_id": "c0"}}, ["exhibition_kind"]),
    ("오늘 관람 시간, 입장료, 공식 링크 알려줘", {"hours": {"value": "9:30", "tool_call_id": "c0"}}, ["fee", "official_link"]),
    ("휴관일만 알려줘", {"closure": {"value": "월 1회", "tool_call_id": "nope"}}, ["closure"]),
])
def test_paraphrased_item_requests(req, items, missing):
    cov = item_coverage(req, items, {"c0"})
    assert sorted(k for k, v in cov.items() if v["status"] in {"missing", "unlinked"}) == sorted(missing)


def test_unrequested_condition_and_repeats_are_classified():
    cov = item_coverage("관람 시간 알려줘", {"hours": {"value": "9:30", "tool_call_id": "c0"}}, {"c0"})
    causes = classify_gap(cov, "", ["관람료도 포함한다"], "관람 시간 알려줘", 3)
    assert "unrequested_condition:fee" in causes and "repeated_lookup:3" in causes


def test_refetch_of_a_page_already_read_with_content_is_refused_but_empty_one_allowed():
    from nanobot.agent.context_selection import EvidenceToolRegistry
    from nanobot.agent.tools.base import Tool

    class Fetch(Tool):
        name, description, read_only = "web_fetch", "f", True
        parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

        async def execute(self, **kw):
            return ""
    rec, sel = _d1()
    reg = EvidenceToolRegistry(sel)
    reg.register(Fetch())
    url = rec["tool_calls"][2]["params"]["url"]
    _, _, err = reg.prepare_call("web_fetch", {"url": url + "/"})
    assert err and "already fetched" in err
    for o in sel.observations.values():
        if o.get("url"):
            o["_text"] = ""
    _, _, err = reg.prepare_call("web_fetch", {"url": url})
    assert not err
    assert sel.trace.counters["fetch_repeats"] == 2

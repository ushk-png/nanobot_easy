"""Relation-level obligations (selective context only): action x target x necessity x condition.

Covers pt22u items 2-4: the validator reroute carries a reinterpretation and unresolved conditions,
never a permission; mutations wait for target / evidence / permission; prior answers bind to the real
response id; deictic external targets bind to exactly one recent artifact or need clarification.
"""
import json

import pytest

from nanobot.agent.context_relations import Rel, bind_targets, parse_relations, recent_artifacts
from nanobot.agent.context_selection import error_class, validate_meaning

from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401


def rels(text):
    return {(r.action, r.target, r.necessity) for r in parse_relations(text)}


# ---- parser: verb classes, polarity, condition, target (paraphrase families) ----

@pytest.mark.parametrize("text", ["파일은 수정하지 말고 답변만 수정해 줘", "파일은 건드리지 말고 답변만 수정해",
                                  "파일은 손대지 말고 위 답변만 고쳐 줘"])
def test_answer_edit_required_file_edit_forbidden(text):
    found = rels(text)
    assert ("edit", "prior_answer", "required") in found
    assert any(a == "edit" and t == "external_resource" and n == "forbidden" for a, t, n in found)


@pytest.mark.parametrize("text", ["TCP와 UDP 차이를 알려줘. 필요하면 검색해", "필요 시 웹 서치 후 답변 바람",
                                  "모르면 찾아보고 답해줘", "필요하면 구글링해서 알려줘", "if needed, look it up"])
def test_conditional_lookup_family(text):
    assert ("lookup", "web", "conditional") in rels(text)


@pytest.mark.parametrize("text", ["구글링해서 알려줘", "웹에서 찾아보고 알려줘", "최신 자료 리서치해서 정리해줘",
                                  "인터넷에서 확인해서 알려줘", "네이버에 쳐보고 알려줘", "look it up and tell me",
                                  "please search the latest RFC", "최신 버전 좀 알아봐 줘"])
def test_required_lookup_family(text):
    assert ("lookup", "web", "required") in rels(text)


@pytest.mark.parametrize("text", ["검색하지 말고 TCP와 UDP 차이를 설명해 줘", "웹은 찾아보지 말고 아는 대로 설명해",
                                  "구글링 없이 설명해 줘"])
def test_forbidden_lookup_family(text):
    assert ("lookup", "web", "forbidden") in rels(text)


@pytest.mark.parametrize("text", ["로그에 '검색 실패'라고 나오는 이유를 설명해 줘", "pytest로 테스트하는 방법을 설명해 줘",
                                  "검색 기능을 설명해 줘", "search 결과가 왜 비었는지 설명해", "테스트 주도 개발을 설명해줘"])
def test_topic_words_quotes_and_adnominals_are_not_relations(text):
    assert rels(text) == set()


def test_unneeded_lookup_is_optional_not_required():
    assert rels("인터넷 검색은 필요 없습니다. 리스트와 튜플 차이를 알려주세요.") == {("lookup", "web", "optional")}


# ---- binding: prior answer id, deictic artifacts ----

def _sources(with_answer=True):
    out = [{"event_id": "u1", "actor": "user", "content": "골프 계획", "sequence": 1}]
    if with_answer:
        out.append({"event_id": "a1", "actor": "assistant", "content": "퍼팅 20분", "sequence": 2})
        out.append({"event_id": "a0", "actor": "assistant", "content": "old", "sequence": 0})
    return out


def test_prior_answer_binds_to_latest_real_response_id():
    found = parse_relations("방금 답변에서 아이언 목표를 수정해 줘")
    bind_targets(found, _sources(), [], "방금 답변에서 아이언 목표를 수정해 줘")
    edit = next(r for r in found if r.target == "prior_answer")
    assert edit.bound_id == "a1" and edit.resolved
    found = parse_relations("방금 답변에서 아이언 목표를 수정해 줘")
    bind_targets(found, _sources(with_answer=False), [], "")
    assert not found[0].resolved and found[0].bound_id is None


@pytest.mark.parametrize(("text", "artifacts", "bound"), [
    ("아까 만든 문서를 수정해", [{"path": "notes.md", "kind": "created"}], "notes.md"),
    ("아까 만든 문서를 수정해", [{"path": "notes.md", "kind": "created"}, {"path": "plan.md", "kind": "created"}], None),
    ("아까 만든 문서를 수정해", [], None),
    ("그 설정 바꿔 줘", [{"path": "notes.md", "kind": "created"}, {"path": "app.yaml", "kind": "modified"}], "app.yaml"),
    ("그 설정 바꿔 줘", [{"path": "a.yaml", "kind": "read"}, {"path": "b.toml", "kind": "read"}], None),
])
def test_deictic_external_target_binds_to_one_artifact_or_stays_unresolved(text, artifacts, bound):
    found = parse_relations(text)
    bind_targets(found, [], artifacts, text)
    rel = next(r for r in found if r.target == "external_resource")
    assert rel.necessity == "required"  # still an external mutation request, never an answer edit
    assert rel.bound_id == bound and rel.resolved is (bound is not None)


# ---- validation: polarity conflict is a model error; parser disagreement is a validator reroute ----

def _payload(request, sources=None, artifacts=()):
    sources = sources if sources is not None else _sources()
    return {"request": request, "sources": [*sources, {"event_id": "cur", "actor": "user", "content": request,
                                                       "sequence": 9}],
            "capabilities": [{"name": "web_search"}, {"name": "change_test_file"}], "task_candidates": [],
            "references": {}, "known_material_clues": [], "recent_artifacts": list(artifacts)}


def test_model_relation_with_wrong_polarity_is_repaired():
    from nanobot.agent.context_selection import Obligations, RelationSpec
    req = "파일은 수정하지 말고 답변만 수정해 줘"
    design = plan(answer="수정된 답변", obligations=Obligations(relations=[
        RelationSpec(action="edit", target="external_resource", necessity="required",
                     source_span="파일은 수정하지 말고")]))
    errors = validate_meaning(design, _payload(req))
    assert "relation_polarity_conflict" in errors and error_class("relation_polarity_conflict") == "plan_conflict"


@pytest.mark.parametrize("req", ["필요하면 검색해", "TCP와 UDP 차이를 알려줘. 필요 시 서치해 줘", "파일은 수정하지 말고 답변만 수정해 줘",
                                 "답변에서 수정해 줘", "위 답변만 고쳐 줘"])
def test_untyped_conditional_and_answer_only_edits_stay_direct(req):
    assert validate_meaning(plan(answer="완전한 답변 본문입니다."), _payload(req)) == []


@pytest.mark.parametrize("req", ["구글링해서 알려줘", "아까 만든 문서를 수정해", "그 설정 바꿔 줘", "답변에서 수정해 줘"])
def test_work_or_unresolved_target_reroutes_without_repair(req):
    sources = [] if req == "답변에서 수정해 줘" else None  # no earlier answer: prior target unresolved
    errors = validate_meaning(plan(answer="완전한 답변 본문입니다."), _payload(req, sources))
    assert errors == ["relation_requires_work"] and error_class(errors[0]) == "validator"


# ---- runtime: reinterpretation, unresolved conditions, mutation gate, typed remaining reasons ----

@pytest.mark.asyncio
async def test_answer_only_edit_completes_and_blocks_file_mutation(env):
    event(env, "골프 60분 계획 세워 줘")
    event(env, "퍼팅 20분, 어프로치 20분, 아이언 20분", actor="assistant")
    req = "파일은 수정하지 말고 답변만 수정해 줘"
    event(env, req)
    _, provider, selected = await select(env, plan(answer="퍼팅 20분, 어프로치 25분, 아이언 15분"), request=req)
    assert selected.blocked is None and len(provider.requests) == 1 and selected.design.route == "direct"
    edit = next(r for r in selected.relations().relations if r.target == "prior_answer")
    assert edit.bound_id and edit.bound_id.startswith("E")  # the real previous response event id
    assert selected.answer_only()
    text, status = selected.verdict("퍼팅 20분, 어프로치 25분, 아이언 15분")
    assert status == "completed"
    selected.tools.register(env[2].get("change_test_file"))  # as discover_context_capabilities would
    _, _, error = selected.tools.prepare_call("change_test_file", {})
    assert error  # the existing user-constraint policy also forbids it
    assert "external_change_forbidden" in selected.mutation_gate("change_test_file")


@pytest.mark.asyncio
async def test_validator_reroute_carries_reinterpretation_and_blocks_unresolved_mutation(env):
    req = "그 설정 바꿔 줘"
    event(env, req)
    _, provider, selected = await select(env, plan(answer="바꿨습니다"), request=req)
    assert len(provider.requests) == 1 and selected.design.route == "execute"
    context = json.loads(selected.execution_context())
    assert context["reinterpretation"]["validator_codes"] == ["relation_requires_work"]
    codes = {c["code"] for c in context["unresolved_conditions"]}
    assert "target_unresolved" in codes and context["mutation_rule"]
    selected.tools.register(env[2].get("change_test_file"))  # as discover_context_capabilities would
    _, _, error = selected.tools.prepare_call("change_test_file", {})
    assert error and "target_unresolved" in error
    text, status = selected.verdict("바꿨습니다")
    # t26 h2: an unidentifiable target is a question to the user (awaiting confirmation), not a failure
    assert status == "awaiting_confirmation" and "그 설정" in text and "아직 아무것도 변경하지" in text


@pytest.mark.asyncio
async def test_deictic_document_resolves_against_session_tool_artifact(env):
    store, scope, *_ = env
    event(env, "회의 메모를 notes.md로 저장해 줘")
    store.append_event(scope=scope, conversation_id="s", session_id="s", actor="tool", event_type="TOOL_RESULT",
                       content="saved", metadata={"name": "write_file", "tool_arguments": {"path": "notes.md"},
                                                  "read_only": False, "status": "ok"})
    event(env, "notes.md에 저장했습니다.", actor="assistant")
    req = "아까 만든 문서를 수정해"
    event(env, req)
    _, _, selected = await select(env, plan(route="execute", answer=None, request_type="modify"), request=req)
    rel = next(r for r in selected.relations().relations if r.action == "edit")
    assert rel.bound_id == "notes.md" and rel.resolved
    assert not [c for c in selected.unresolved_conditions() if c["kind"] == "target"]
    selected.tools.register(env[2].get("change_test_file"))  # as discover_context_capabilities would
    _, _, error = selected.tools.prepare_call("change_test_file", {})
    assert error is None  # requested, bound, nothing forbidden: the mutation may run
    assert not selected.answer_only()
    text, status = selected.verdict("수정했습니다")
    assert status == "incomplete" and "requirement_unmet:external_change" in text


@pytest.mark.asyncio
async def test_reinterpretation_is_never_permission_without_requested_change(env):
    req = "변경 사항을 검증해 줘"  # inspect: work, but no external change was requested
    event(env, req)
    _, _, selected = await select(env, plan(answer="검증했습니다"), request=req)
    assert selected.reinterpretation and selected.design.route == "execute"
    selected.tools.register(env[2].get("change_test_file"))  # as discover_context_capabilities would
    _, _, error = selected.tools.prepare_call("change_test_file", {})
    assert error and "no_requested_external_change" in error
    _, _, read_error = selected.tools.prepare_call("read_file", {})
    assert read_error is None  # read-only work stays allowed


@pytest.mark.asyncio
async def test_conditional_lookup_completes_without_search_and_required_lookup_does_not(env):
    req = "TCP와 UDP 차이를 알려줘. 필요하면 검색해"
    event(env, req)
    _, provider, selected = await select(env, plan(answer="TCP는 연결 지향, UDP는 비연결."), request=req)
    assert len(provider.requests) == 1 and selected.design.route == "direct"
    assert selected.verdict("TCP는 연결 지향, UDP는 비연결.")[1] == "completed"
    req = "구글링해서 TCP와 UDP 차이를 알려줘"
    event(env, req)
    _, _, selected = await select(env, plan(answer="TCP는 ..."), request=req)
    text, status = selected.verdict("TCP는 ...")
    assert status == "incomplete" and "requirement_unmet:lookup" in text


def test_recent_artifacts_reads_tool_paths_and_dialogue_files(env):
    store, scope, *_ = env
    store.append_event(scope=scope, conversation_id="s", session_id="s", actor="tool", event_type="TOOL_RESULT",
                       content="ok", metadata={"name": "edit_file", "tool_arguments": {"path": "/w/app.yaml"},
                                               "read_only": False, "status": "ok"})
    event(env, "report.md 파일을 작성했습니다.", actor="assistant")
    found = recent_artifacts(store.list_events(scope, "s") if hasattr(store, "list_events") else
                             __import__("nanobot.memory.task_context", fromlist=["TaskContextStore"])
                             .TaskContextStore(store).events(scope, "s"))
    assert {"app.yaml", "report.md"} <= {a["path"] for a in found}
    assert next(a for a in found if a["path"] == "app.yaml")["kind"] == "modified"


def test_rel_public_keeps_only_set_fields():
    assert Rel("edit", "prior_answer", "required", "답변만 수정해").public()["origin"] == "parser"

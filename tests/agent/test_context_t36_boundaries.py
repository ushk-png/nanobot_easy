"""t36 boundary tests: every case has a correct-allow and a wrong-allow-prevention check, built
from the real t29 design outputs where one exists. They check target selection, tool execution,
state and completion – not output text. No network, no model."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from nanobot.agent.context_control import instruction_state, scope_entry
from nanobot.agent.context_evidence import current, exclusive_create_paths, fingerprint, observation
from nanobot.agent.context_relations import Rel, artifact_identity, bind_targets
from nanobot.agent.context_selection import (IntentDesign, SelectedContext, computed_mismatch,
                                             effective_relations, instruction_entries, select_design_output)
from nanobot.agent.context_trace import ContextTrace
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.providers.base import LLMResponse, ToolCallRequest

FIX = json.loads((Path(__file__).parent / "fixtures" / "t29_design_outputs.json").read_text())
REQ = FIX["requests"]
RO = SimpleNamespace(read_only=True)


def args(label):
    return json.loads(json.dumps(FIX["cases"][label]["tool_calls"][0]["arguments"]))


def sel_for(a, request, ws):
    sel = SelectedContext(trace=ContextTrace("t36"), design=IntentDesign.model_validate(a))
    sel.current_event = SimpleNamespace(content=request, sequence=5, event_id="u5")
    sel.recent, sel.artifacts = [], []
    sel.scope = SimpleNamespace(workspace_ids=[ws])
    return sel


def capture(sel, cid, name, arguments, result, status="ok"):
    sel.capture(SimpleNamespace(id=cid, name=name, arguments=arguments), result, status, RO)
    return sel.observations[cid]


def eff(a, request):
    return effective_relations(IntentDesign.model_validate(a), request, [], [])


# ---- 1. reference across sentences / quoted deletion ----
CROSS = "지정된 시험 폴더에 notes.md를 만들어 주세요. 그 파일에 ‘회의 시간: 10시’를 저장해 주세요."
def cross_design(d):
    r = d["obligations"]["relations"]
    r[0]["source_span"] = "지정된 시험 폴더에 notes.md를 만들어 주세요."
    r[1]["source_span"] = "그 파일에 ‘회의 시간: 10시’를 저장해 주세요."
    r[2]["source_span"] = r[0]["source_span"]
    return d


def test_cross_sentence_reference_is_grounded_and_wrong_target_is_not():
    a = cross_design(args("p2-file-r1-t1"))       # t29 file t1, action in its own sentence ("그 파일에")
    rels = a["obligations"]["relations"]
    request = CROSS
    e = eff(a, request)
    assert not [c for c in e.conflicts if c["code"] == "model_target_ungrounded"]       # allow
    bad = json.loads(json.dumps(a)); bad["obligations"]["relations"][1]["target_ref"] = "/elsewhere/x.md"   # "그 파일" bound elsewhere
    assert [c for c in eff(bad, request).conflicts if c["code"] == "model_reference_unresolved"]  # prevent
    assert rels  # fixture sanity


def test_quoted_deletion_in_same_sentence_does_not_ground_a_delete():
    a = args("p2-file-r1-t1")
    request = '노트에 "notes.md를 삭제해"라는 문장을 적어 두세요.'
    a["obligations"]["relations"] = [{"action": "delete", "target": "external_resource", "necessity": "required",
                                      "source_span": "notes.md를 삭제해", "target_ref": "notes.md", "condition": ""}]
    e = eff(a, request)
    assert not [r for r in e.relations if r.action == "delete" and r.necessity == "required"
                and r.resolved and r.bound_id] or e.conflicts                         # prevent
    a2 = args("p2-file-r1-t1")
    a2["obligations"]["relations"] = [{"action": "delete", "target": "external_resource", "necessity": "required",
                                       "source_span": "notes.md를 삭제해", "target_ref": "notes.md", "condition": ""}]
    e2 = eff(a2, "notes.md를 삭제해 주세요.")
    assert not [c for c in e2.conflicts if c["code"] == "model_target_ungrounded"]       # allow


# ---- 2. lookups: another page, repeated fetch, material present but link rejected ----

PAGE = json.dumps({"url": "https://www.museum.go.kr/a", "status": 200, "text": "관람시간 " * 80})


def test_other_page_of_same_site_is_allowed_identical_fetch_is_reused(tmp_path):
    sel = sel_for(args("p2-museum-r1-t1"), REQ["MUSEUM"], str(tmp_path))
    capture(sel, "f1", "web_fetch", {"url": "https://www.museum.go.kr/a"}, PAGE)
    assert sel.repeated_lookup("web_fetch", {"url": "https://www.museum.go.kr/b"}) is None        # allow
    msg = sel.repeated_lookup("web_fetch", {"url": "https://www.museum.go.kr/a"})
    assert msg and "f1" in msg and "reused" in msg                                                # prevent


def test_rejected_page_is_not_refetched_but_transport_is_separate(tmp_path):
    sel = sel_for(args("p2-museum-r1-t1"), REQ["MUSEUM"], str(tmp_path))
    capture(sel, "f1", "web_fetch", {"url": "https://x.kr/h"}, '{"error": "", "url": "https://x.kr/h"}')
    assert "fetch_rejected" in sel.repeated_lookup("web_fetch", {"url": "https://x.kr/h"})        # prevent
    tls = "Error: DuckDuckGo search failed (RequestError: tls handshake eof)"
    capture(sel, "s1", "web_search", {"query": "박물관 관람료"}, tls)
    assert sel.repeated_lookup("web_search", {"query": "박물관 관람료"}) is None                 # allow 1 retry
    capture(sel, "s2", "web_search", {"query": "박물관 관람료"}, tls)
    msg = sel.repeated_lookup("web_search", {"query": "박물관 관람료"})
    assert msg and "transport" in msg and "says nothing" in msg                                   # resource limit


def test_material_present_link_rejected_is_a_judgment_error(tmp_path):
    sel = sel_for(args("p2-museum-r1-t1"), REQ["MUSEUM"], str(tmp_path))
    capture(sel, "f1", "web_fetch", {"url": "https://www.museum.go.kr/a"}, PAGE)
    sel.note_judgment("resolve_context_need", "requested items not yet linked: fee", ["f1"])
    assert sel.judgment_error and sel.judgment_error["material_calls"] == ["f1"]                # prevent re-search
    sel.note_judgment("resolve_context_need", "requested items not yet linked: fee", ["nothing"])
    assert sel.judgment_error is None                                                             # no material: search allowed


# ---- 3. design function call ----

def test_multiple_calls_same_design_vs_real_conflict():
    a = args("p2-golf-r1-t1")
    reordered = dict(reversed(list(a.items())))
    same = LLMResponse(content=None, tool_calls=[ToolCallRequest(id="0", name="submit_context_design", arguments=a),
                                                 ToolCallRequest(id="1", name="submit_context_design", arguments=reordered)])
    assert select_design_output(same)[2] == []                                                    # allow
    other = LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="0", name="submit_context_design", arguments=a),
        ToolCallRequest(id="1", name="submit_context_design", arguments=args("p2-golf-r1-t2"))])
    assert select_design_output(other)[2] == ["design_conflicting_outputs"]                       # prevent


# ---- 4. files: new creation target exists / same version already read ----

def _file_design(tmp_path):
    a = json.loads(json.dumps(args("p2-file-r1-t1")).replace("/workspace/t29/runs/ws/p2-file-r1", str(tmp_path)))
    return sel_for(a, REQ["FILE1"], str(tmp_path))


def test_creation_target_absent_is_created_existing_is_never_overwritten(tmp_path):
    sel = _file_design(tmp_path)
    targets = sel.creation_targets()
    assert targets, "file t1 requires creating notes.md"
    target = sorted(targets)[0]
    assert sel.creation_conflict("write_file", {"path": target, "content": "a"}) is None           # allow
    tool = WriteFileTool(workspace=tmp_path)
    tok = exclusive_create_paths.set(frozenset(targets))
    try:
        assert "Successfully" in str(asyncio.run(tool.execute(path=target, content="a")))
        capture_ok = {"tool_call_id": "w1", "status": "ok", "read_only": False, "resource_id": target}
        sel.observations["w1"] = capture_ok
        assert sel.creation_conflict("write_file", {"path": target, "content": "b"}) is None       # own creation
        del sel.observations["w1"]
        assert sel.creation_conflict("write_file", {"path": target, "content": "b"})              # prevent
        out = str(asyncio.run(tool.execute(path=target, content="b")))
        assert "already exists" in out and Path(target).read_text() == "a"                        # not overwritten
    finally:
        exclusive_create_paths.reset(tok)


def test_same_version_read_needs_no_reread_changed_version_does(tmp_path):
    f = tmp_path / "plan.md"; f.write_text("v1\n")
    read = ReadFileTool(workspace=tmp_path)
    first = str(asyncio.run(read.execute(path=str(f))))
    assert "v1" in first
    assert "unchanged" in str(asyncio.run(read.execute(path=str(f))))                              # allow reuse
    sel = _file_design(tmp_path)
    rec = observation(sel, SimpleNamespace(id="r1", name="read_file", arguments={"path": str(f)}), first, "ok", RO)
    rec.update(resource_id=str(f.resolve()), version=fingerprint(f))   # as the read coverage records it
    assert current(rec)
    f.write_text("v2 changed\n")
    assert not current(rec)                                                                        # prevent stale edit
    assert "v2" in str(asyncio.run(read.execute(path=str(f))))


# ---- 5. calculations ----

def test_single_computed_value_right_and_wrong():
    spec = {"values": {"1인당": 8000}, "total": 48000, "sum_parts": []}
    assert computed_mismatch(spec, "48,000원 ÷ 6명 = 1인당 8,000원") is None                        # allow
    assert computed_mismatch(spec, "48,000원 ÷ 6명 = 1인당 9,000원")                                 # prevent
    undeclared = {"values": {"1인당": 8000}, "total": 48000}
    assert computed_mismatch(undeclared, "1인당 8,000원, 총 48,000원")                               # no skip for one value


def test_missing_is_not_zero_and_explicit_exclusion():
    spec = {"values": {"퍼팅": 20, "아이언": 25}, "total": 45, "excluded": ["드라이버"], "not_allocated": ["드라이버"]}
    assert computed_mismatch(spec, "퍼팅 20분\n아이언 25분\n합계 45분") is None                       # allow
    assert computed_mismatch(spec, "퍼팅 20분\n아이언 25분\n드라이버 10분\n합계 45분")                 # prevent
    missing = {"values": {"퍼팅": 20, "아이언": None}, "total": 45}
    assert computed_mismatch(missing, "퍼팅 20분\n아이언 0분\n합계 45분").startswith("computed_required_value_missing")
    zero = {"values": {"퍼팅": 45, "아이언": 0}, "total": 45}
    assert computed_mismatch(zero, "퍼팅 45분\n아이언 0분\n합계 45분") is None                         # a real 0


# ---- 6. directories: alias and same-named files ----

def test_path_alias_is_one_target_same_names_stay_apart(tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    (tmp_path / "a" / "todo.md").write_text("1"); (tmp_path / "b" / "todo.md").write_text("2")
    (tmp_path / "link").symlink_to(tmp_path / "a")
    ws = str(tmp_path)
    assert artifact_identity("link/todo.md", ws) == artifact_identity(str(tmp_path / "a" / "todo.md"), ws)  # allow
    assert artifact_identity("a/todo.md", ws) != artifact_identity("b/todo.md", ws)                          # prevent
    rel = Rel(action="write", target="external_resource", necessity="required", source_span="그 파일", target_ref="그 파일")
    alias = [{"path": "a/todo.md", "kind": "created"}, {"path": "link/todo.md", "kind": "created"}]
    bind_targets([rel], [], alias, "그 파일 수정해 줘", workspace=ws)
    assert rel.resolved and rel.bound_id                                                          # one target
    rel2 = Rel(action="write", target="external_resource", necessity="required", source_span="그 파일", target_ref="그 파일")
    two = [{"path": "a/todo.md", "kind": "created"}, {"path": "b/todo.md", "kind": "created"}, {"path": "todo.md"}]
    bind_targets([rel2], [], two, "그 파일 수정해 줘", workspace=ws)
    assert not rel2.resolved and len(rel2.candidates) == 2                                        # ask


# ---- 7. follow-up permission / conflicting instructions with unknown order ----

def test_explicit_follow_up_permission_and_unordered_conflict():
    payload = {"request": REQ["PERM2"], "sources": [{"event_id": "u1", "sequence": 1, "actor": "user"}],
               "task_state": {"valid_constraint_candidates": [{"source_id": "u1", "text": REQ["PERM1"],
                                                                "status": "user_original"}]}}
    scope, unknown = instruction_state(instruction_entries(payload))
    assert not unknown                                                                            # ordered: allow
    denial, grant = scope_entry(None, REQ["PERM1"]), scope_entry(None, REQ["PERM2"])
    scope, unknown = instruction_state([denial, grant])
    assert unknown and not (unknown & set(scope))                                                 # held, ask
    scope, unknown = instruction_state([denial])
    assert not unknown and scope                                                                  # lone denial stands

"""t28 contrast: correct / wrong / parser-unrecognised designs for each request; gates,
targets and completion judgment; over-block and wrong-allow both checked. No network."""
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from nanobot.agent.context_selection import IntentDesign, SelectedContext
from nanobot.agent.context_trace import ContextTrace
from nanobot.agent import context_control as cc

PRIOR = {"event_id": "a1", "actor": "assistant", "sequence": 1,
         "content": "이전 답변입니다. 첫째 줄. 둘째 줄. 셋째 줄. 넷째 줄."}
DOC = [{"path": "report.md", "full_path": "/ws/report.md", "kind": "created", "event_id": "t1"}]


def R(action, target, necessity, span, ref=None):
    return {"action": action, "target": target, "necessity": necessity, "source_span": span, "target_ref": ref}


def design(route, rels, *, lookup="unneeded", ext="none", target="answer", span=None):
    return IntentDesign.model_validate(dict(
        goal="g", request_type="modify" if ext == "required" else "explain", task_relation="new", task_id=None,
        route=route, constraints=[], information_needs=[], capabilities_needed=[],
        completion_checks=["done"], unresolved=[], answer="답" if route == "direct" else None,
        obligations={"target": target, "lookup": lookup, "external_change": ext, "source_span": span,
                     "relations": rels}))


# expected: write = may a write/edit tool touch `path`; web = may web_search run;
# answer_only = may the turn complete with an answer and no tool evidence.
CASES = [
 ("C01", "방금 답변을 절반으로 줄여 줘.", None, dict(write=False, web=None, answer_only=True),
  lambda r: design("direct", [R("edit", "prior_answer", "required", r)], target="prior_answer", span=r),
  lambda r: design("execute", [R("edit", "external_resource", "required", r, "답변")], ext="required", target="external_resource", span=r)),
 ("C02", "아까 만든 문서 파일의 내용을 절반으로 줄여 줘.", "report.md", dict(write=True, web=None, answer_only=False),
  lambda r: design("execute", [R("edit", "external_resource", "required", r, "아까 만든 문서")], ext="required", target="external_resource", span=r),
  lambda r: design("direct", [R("edit", "prior_answer", "required", r)], target="prior_answer", span=r)),
 ("C03", "이 대화에 세 줄로 적어 줘.", None, dict(write=False, web=None, answer_only=True),
  lambda r: design("direct", [R("write", "answer", "required", r)], span=r),
  lambda r: design("execute", [R("write", "external_resource", "required", r, "대화")], ext="required", target="external_resource", span=r)),
 ("C04", "notes.md에 세 줄로 적어 줘.", "notes.md", dict(write=True, web=None, answer_only=False),
  lambda r: design("execute", [R("write", "external_resource", "required", r, "notes.md")], ext="required", target="external_resource", span=r),
  lambda r: design("direct", [R("write", "answer", "required", r)], span=r)),
 ("C05", "검색하지 말고 설명해 줘.", None, dict(write=False, web=False, answer_only=True),
  lambda r: design("direct", [R("lookup", "web", "forbidden", "검색하지 말고")], lookup="forbidden", span="검색하지 말고"),
  lambda r: design("retrieve", [R("lookup", "web", "required", "검색하지 말고")], lookup="required", span="검색하지 말고")),
 ("C06", "필요하면 검색해 줘.", None, dict(write=False, web=True, answer_only=True),
  lambda r: design("direct", [R("lookup", "web", "conditional", r)], lookup="conditional", span=r),
  lambda r: design("retrieve", [R("lookup", "web", "required", r)], lookup="required", span=r)),
 ("C07", "구글링해서 알려줘.", None, dict(write=False, web=True, answer_only=False),
  lambda r: design("retrieve", [R("lookup", "web", "required", "구글링해서")], lookup="required", span="구글링해서"),
  lambda r: design("direct", [R("lookup", "web", "forbidden", "구글링해서")], lookup="forbidden", span="구글링해서")),
 ("C08", "파일은 수정하지 말고 답변만 수정해 줘.", None, dict(write=False, web=None, answer_only=True),
  lambda r: design("direct", [R("edit", "external_resource", "forbidden", "파일은 수정하지 말고", "파일"),
                              R("edit", "prior_answer", "required", "답변만 수정해 줘")], target="prior_answer", span="답변만 수정해 줘"),
  lambda r: design("execute", [R("edit", "external_resource", "required", "파일은 수정하지 말고", "파일")], ext="required", target="external_resource", span="파일은 수정하지 말고")),
 ("C09", "‘파일을 삭제하세요’를 정중한 표현으로 바꿔 줘.", None, dict(write=False, web=None, answer_only=True, delete=False),
  lambda r: design("direct", [R("edit", "answer", "required", "정중한 표현으로 바꿔 줘")], span="정중한 표현으로 바꿔 줘"),
  lambda r: design("execute", [R("delete", "external_resource", "required", "파일을 삭제하세요", "파일")], ext="required", target="external_resource", span="파일을 삭제하세요")),
 ("C10", "문서 파일 안의 ‘삭제하세요’라는 문구만 ‘보관하세요’로 바꿔 줘.", "report.md", dict(write=True, web=None, answer_only=False, delete=False),
  lambda r: design("execute", [R("edit", "external_resource", "required", r, "문서 파일")], ext="required", target="external_resource", span=r),
  lambda r: design("execute", [R("delete", "external_resource", "required", "삭제하세요", "문서 파일")], ext="required", target="external_resource", span="삭제하세요")),
 ("C11", "You may now edit notes.md.", "notes.md", dict(write=True, web=None, answer_only=None),
  lambda r: design("execute", [R("edit", "external_resource", "required", r, "notes.md")], ext="required", target="external_resource", span=r),
  lambda r: design("direct", [R("edit", "external_resource", "forbidden", r, "notes.md")], span=r)),
 ("C12", "Do not edit notes.md; revise your answer only.", "notes.md", dict(write=False, web=None, answer_only=True),
  lambda r: design("direct", [R("edit", "external_resource", "forbidden", "Do not edit notes.md", "notes.md"),
                              R("edit", "prior_answer", "required", "revise your answer only")], target="prior_answer", span="revise your answer only"),
  lambda r: design("execute", [R("edit", "external_resource", "required", "Do not edit notes.md", "notes.md")], ext="required", target="external_resource", span="Do not edit notes.md")),
 ("C13", "Save this text to notes.md.", "notes.md", dict(write=True, web=None, answer_only=False),
  lambda r: design("execute", [R("write", "external_resource", "required", r, "notes.md")], ext="required", target="external_resource", span=r),
  lambda r: design("direct", [R("write", "answer", "required", r)], span=r)),
 ("C14", "Write three lines in your reply.", None, dict(write=False, web=None, answer_only=True),
  lambda r: design("direct", [R("write", "answer", "required", r)], span=r),
  lambda r: design("execute", [R("write", "external_resource", "required", r, "reply")], ext="required", target="external_resource", span=r)),
]
# C15: history (deny then explicit grant on the same target)
HISTORY = [("notes.md는 수정하지 마.", 1), ("이제 notes.md를 수정해도 돼.", 2)]


def selected(req, d, artifacts):
    sel = SelectedContext(trace=ContextTrace("probe"), design=d)
    sel.current_event = SimpleNamespace(content=req, sequence=5, event_id="u5")
    sel.recent = [PRIOR]
    sel.artifacts = artifacts
    sel.scope = SimpleNamespace(workspace_ids=["/ws"])
    return sel


def user_scope(texts):
    entries = [(seq, set(cc.instruction_policy(t)["denied_actions"]), cc.granted_actions(t), cc.granted_targets(t))
               for t, seq in texts]
    return cc.effective_scope(entries)


def run_case(cid, req, path, exp, d, variant, blind=False):
    ctx = patch("nanobot.agent.context_selection.parse_relations", lambda *_a, **_k: []) if blind else None
    if ctx: ctx.__enter__()
    try:
        arts = DOC if (path == "report.md") else []
        sel = selected(req, d, arts)
        gate = sel.mutation_gate("write_file")
        scope = user_scope([(req, 5)])
        tgt = path or "notes.md"
        auth_ok = not ("change" in scope and tgt not in scope["change"])
        write_ok = gate is None and auth_ok
        del_scope_blocked = "delete" in scope
        web_ok = sel.lookup_gate("web_search") is None
        try:
            ans = bool(sel.answer_only())
        except Exception as e:
            ans = "error:" + type(e).__name__
        unmet = sel.unmet_reasons()
        rels = [(r.action, r.target, r.necessity, r.origin) for r in sel.relations().relations]
    finally:
        if ctx: ctx.__exit__(None, None, None)
    out = {"id": f"{cid}/{variant}", "write_ok": write_ok, "gate": gate and gate[:80], "web_ok": web_ok,
           "answer_only": ans, "unmet": unmet, "rels": rels}
    issues = []
    if exp["write"] is not None and write_ok != exp["write"]:
        issues.append("over_block:write" if exp["write"] else "wrong_allow:write")
    if exp["web"] is not None and web_ok != exp["web"]:
        issues.append("over_block:web" if exp["web"] else "wrong_allow:web")
    if exp["answer_only"] is not None and ans != exp["answer_only"]:
        issues.append("over_block:answer_requires_evidence" if exp["answer_only"] else "wrong_allow:answer_without_work")
    if variant == "wrong":
        # A wrong design is handled correctly when no wrong allow occurs and the disagreement is
        # marked (conflict/unmet/validation), instead of the design silently deciding the turn.
        flagged = bool([u for u in unmet if not u.startswith("information_missing")]) or (gate or "").find("conflict") >= 0
        issues = [i for i in issues if i.startswith("wrong_allow")]
        if not flagged and out["answer_only"] is True and exp["answer_only"] is False:
            issues.append("wrong_allow:answer_without_work")
        out["flagged"] = flagged
    out["issues"] = issues
    return out




PARAMS = [(c, v) for c in CASES for v in ("correct", "wrong", "parser_blind")]


@pytest.mark.parametrize("case,variant", PARAMS, ids=[f"{c[0]}-{v}" for c, v in PARAMS])
def test_contrast(case, variant):
    cid, req, path, exp, good, bad = case
    span = req.rstrip(".")
    d = bad(span) if variant == "wrong" else good(span)
    row = run_case(cid, req, path, exp, d, variant, blind=variant == "parser_blind")
    assert row["issues"] == [], row
    if variant == "wrong":
        assert row["flagged"], row   # the disagreement is marked, not decided by the design


@pytest.mark.parametrize("history", [HISTORY, [("Do not edit notes.md.", 1), ("You may now edit notes.md.", 2)]])
def test_permission_after_prohibition_reopens_only_that_target(history):
    scope = user_scope(history)
    assert "notes.md" in scope.get("change", {"notes.md"})
    assert "change" in scope and "other.md" not in scope["change"]


def test_quoted_prohibition_is_not_an_instruction():
    assert "delete" not in cc.instruction_policy("‘파일을 삭제하지 마세요’를 정중한 표현으로 바꿔 줘.")["denied_actions"]
    assert "delete" in cc.instruction_policy("파일을 삭제하지 마세요.")["denied_actions"]


def test_unrecognised_stays_unrecognised():
    from nanobot.agent.context_relations import parse_relations
    # 줄여/적어 are not in the verb classes: no relation is invented, and none is turned into allow/forbid
    assert parse_relations("방금 답변을 절반으로 줄여 줘.") == []
    sel = selected("방금 답변을 절반으로 줄여 줘.", design("direct", [], target="answer"), [])
    assert sel.relations().relations == [] and sel.relations().conflicts == []
    assert sel.mutation_gate("write_file") is not None      # no grounded external change
    assert sel.lookup_gate("web_search") is None             # nothing forbids lookup either

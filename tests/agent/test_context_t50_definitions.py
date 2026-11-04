"""t50 test definitions — NOT EXECUTED in t50 (implementation only, unverified).

Covers, per t50 item: correct allowance, prevention of a wrong allowance, follow-up-turn restoration
and recovery after a failure. They use small duck-typed fakes or the t47 MOCK runner harness (scripted
executor, no real model call). Run them only in a later verification step; fixture details may need
adjusting then.
"""
import json
from types import SimpleNamespace

import pytest

from nanobot.agent import context_plan as cp
from nanobot.agent.context_relations import bind_targets, parse_relations


class Trace:
    def __init__(self):
        self.counts = {}
        self.request_id = "req"

    def count(self, key, value=None):
        self.counts.setdefault(key, []).append(value)


class FakeSel:
    """Just what context_plan reads from a SelectedContext."""

    def __init__(self, required=None, versions=None, workspace="/ws"):
        self.exec_plan, self.trace, self.workspace = {}, Trace(), workspace
        self.instruction_version, self.state, self.control, self.design = 1, None, None, None
        self.completion_reports, self.check_bindings, self.settled_unresolved = {}, {}, {}
        self.observations, self._required, self.versions = {}, required or [], versions or {}

    def required_results(self):
        return self._required

    def target_version(self, target):
        return self.versions.get(target)

    def check_stale(self, check):
        return False


def call(cid, name, args):
    return SimpleNamespace(id=cid, name=name, arguments=args)


def rec(status="ok", read_only=True, target=None, failure=None, executed=True, out="h"):
    return {"status": status, "read_only": read_only, "resource_id": target, "failure_class": failure,
            "executed": executed, "output_hash": out}


def _selected(required=(), conditions=(), request="", observations=None):
    from nanobot.agent.context_selection import ContextTrace, SelectedContext
    sel = SelectedContext(trace=ContextTrace(request_id="req"))
    sel.design = SimpleNamespace(unresolved=[], task_id=None, task_relation="new", completion_checks=[])
    sel.current_event = SimpleNamespace(content=request, event_id="e-now")
    sel.required_results = lambda: list(required)
    sel.unresolved_conditions = lambda: list(conditions)
    sel.observations = observations or {}
    return sel


# ---- item 1: write obligations come from linked requirements only --------------------------------------

def test_path_in_request_alone_is_not_a_write_obligation():
    """correct allowance: a path quoted as an example; a blocked write to it is optional information."""
    sel = _selected(request="'docs/example.md' 같은 형식으로 설명해 줘", observations={
        "w": {"name": "write_file", "status": "blocked", "read_only": False, "resource_id": "/ws/docs/example.md",
              "executed": False, "block_reason": "no grounded change"}})
    required, optional = sel.unperformed_changes()
    assert required == [] and optional and optional[0].startswith("optional_change_not_performed:")


def test_blocked_write_linked_to_required_requirement_blocks_completion():
    """wrong-allowance prevention: the target of a valid required change stays required."""
    sel = _selected(required=[{"kind": "file_change", "target": "/ws/t47_c.md"}], observations={
        "w": {"name": "write_file", "status": "blocked", "read_only": False, "resource_id": "/ws/t47_c.md",
              "executed": False, "block_reason": "blocked"}})
    required, _ = sel.unperformed_changes()
    assert required and required[0].startswith("required_change_not_performed:/ws/t47_c.md")


def test_unknown_outcome_change_is_not_reported_as_not_performed():
    sel = _selected(required=[{"kind": "file_change", "target": "/ws/a.md"}], observations={
        "w": {"name": "write_file", "status": "unknown", "read_only": False, "resource_id": "/ws/a.md"}})
    required, _ = sel.unperformed_changes()
    assert required[0].startswith("required_change_outcome_unknown:")


def test_omission_check_rides_in_the_existing_execution_context():
    from nanobot.agent.context_selection import FINAL_ANSWER_RULE
    sel = _selected()
    sel.design = None
    ctx = json.loads(sel.execution_context())
    assert ctx["final_answer_rule"] == FINAL_ANSWER_RULE and "supplement_requirement" in FINAL_ANSWER_RULE


# ---- item 2: same work vs different work ----------------------------------------------------------------

def test_notation_only_change_is_the_same_attempt():
    a = cp.identity("write_file", {"path": "./a.md", "content": "x"}, "/ws")
    b = cp.identity("write_file", {"path": "/ws/a.md", "content": "x", "intent_summary": "other"}, "/ws")
    assert a == b
    assert cp.identity("web_search", {"query": "Foo  Bar"}) == cp.identity("web_search", {"query": "foo bar"})


def test_title_then_body_are_different_changes():
    sel = FakeSel(versions={"/ws/a.md": "v1"})
    cp.record_fact(sel, call("t", "edit_file", {"path": "/ws/a.md", "old": "", "new": "# Title"}),
                   rec(read_only=False, target="/ws/a.md"))
    assert cp.blocked_repeat(sel, "edit_file", {"path": "/ws/a.md", "old": "body", "new": "Body"}) is None
    assert cp.blocked_repeat(sel, "edit_file", {"path": "./a.md", "old": "", "new": "# Title"}) is not None


def test_unrelated_material_does_not_release_a_no_progress_attempt():
    sel = FakeSel(required=[{"kind": "file_change", "target": "/ws/a.md"}], versions={"/ws/a.md": "v1"})
    for i in range(2):
        cp.record_fact(sel, call("f%d" % i, "write_file", {"path": "/ws/a.md", "content": "x"}),
                       rec(status="error", read_only=False, target="/ws/a.md", failure="tool_error"))
    cp.record_fact(sel, call("r", "read_file", {"path": "/ws/other.md"}), rec(target="/ws/other.md", out="o"))
    assert cp.blocked_repeat(sel, "write_file", {"path": "/ws/a.md", "content": "x"}) is not None   # still blocked
    cp.record_fact(sel, call("r2", "read_file", {"path": "/ws/a.md"}), rec(target="/ws/a.md", out="n"))
    assert cp.blocked_repeat(sel, "write_file", {"path": "/ws/a.md", "content": "x"}) is None       # relevant material


def test_interrupted_change_needs_a_read_then_stays_undetermined_until_decided():
    """recovery after failure: an interrupted write is outcome-unknown; a read of the target is required
    before the same write, and the read itself does not declare success or failure."""
    sel = FakeSel(versions={"/ws/a.md": "v1"})
    cp.record_fact(sel, call("w", "write_file", {"path": "/ws/a.md", "content": "x"}),
                   rec(status="unknown", read_only=False, target="/ws/a.md", failure="tool_error"))
    assert "without a known outcome" in cp.blocked_repeat(sel, "write_file", {"path": "/ws/a.md", "content": "x"})
    cp.record_fact(sel, call("r", "read_file", {"path": "/ws/a.md"}), rec(target="/ws/a.md"))
    assert cp.blocked_repeat(sel, "write_file", {"path": "/ws/a.md", "content": "x"}) is None


# ---- item 3: unresolved conditions hold only related work -----------------------------------------------

TARGET_COND = {"kind": "target", "code": "target_unresolved", "action": "edit", "target": "external_resource",
               "target_ref": "그 설정 파일", "candidates": ["/ws/plan.md", "/ws/app.yaml"]}


def test_independent_change_runs_while_another_target_is_unresolved():
    sel = _selected(required=[{"kind": "file_change", "target": "/ws/notes.md"}], conditions=[TARGET_COND])
    assert sel.mutation_gate("write_file", "/ws/notes.md") is None


def test_change_on_a_candidate_or_unknown_target_is_held():
    sel = _selected(required=[{"kind": "file_change", "target": "/ws/plan.md"}], conditions=[TARGET_COND])
    assert sel.mutation_gate("write_file", "/ws/plan.md")          # one of the open choices
    assert sel.mutation_gate("exec")                               # no target known: not independent


def test_permission_condition_holds_every_change():
    perm = {"kind": "permission", "code": "external_change_forbidden", "source_span": "파일은 건드리지 마"}
    sel = _selected(required=[{"kind": "file_change", "target": "/ws/notes.md"}], conditions=[perm])
    assert sel.mutation_gate("write_file", "/ws/notes.md")


def test_design_item_scope_unknown_holds_changes_and_scoped_item_releases_others():
    unknown = {"kind": "target", "code": "design_unresolved", "text": "단위 불명확", "scope": "unknown"}
    scoped = {"kind": "target", "code": "design_unresolved", "text": "단위 불명확", "affects": ["answer"]}
    req = [{"kind": "file_change", "target": "/ws/notes.md"}]
    assert "settle_unresolved" in _selected(required=req, conditions=[unknown]).mutation_gate("write_file", "/ws/notes.md")
    assert _selected(required=req, conditions=[scoped]).mutation_gate("write_file", "/ws/notes.md") is None


def test_answer_fix_not_stopped_by_change_only_condition(monkeypatch):
    sel = FakeSel()
    sel.design = object()
    sel.unresolved_conditions = lambda: [TARGET_COND]
    monkeypatch.setattr(cp, "answer_fix_feedback", lambda s, a: "fix total")
    assert cp.fix_route(sel, "합계 10", True) == ("answer", "fix total")


def test_answer_fix_held_when_item_decides_the_answer(monkeypatch):
    sel = FakeSel()
    sel.design = object()
    sel.unresolved_conditions = lambda: [{"code": "design_unresolved", "text": "통화 단위", "affects": ["answer"]}]
    monkeypatch.setattr(cp, "answer_fix_feedback", lambda s, a: "fix total")
    assert cp.fix_route(sel, "합계 10", True) == ("clarify", None)


def test_permission_doubt_is_not_narrowed_by_affects():
    from nanobot.agent.context_selection import settle_unresolved
    sel = _selected(required=[{"kind": "file_change", "target": "/ws/notes.md"}], request="notes.md에 적어 줘")
    sel.design.unresolved = ["변경 권한 불명확"]
    out = settle_unresolved(sel, [{"text": "변경 권한 불명확", "disposition": "unclear_fact", "aspect": "permission",
                                   "basis_span": "적어 줘", "affects": ["file_change:/ws/notes.md"]}])
    assert getattr(out, "is_error", False) and not sel.unresolved_scope


def test_partial_report_names_the_held_part_and_is_not_completed():
    """verdict: with one change done and one target unresolved -> status incomplete, executed change listed,
    '확인 필요(해당 작업은 보류)' with the decision; with no change but an independent answer ->
    awaiting_confirmation with '독립적으로 처리한 부분(미검증)'."""
    pytest.skip("definition only: needs the MOCK runner harness with an ambiguous second target")


# ---- item 4: follow-up restoration from the existing task record ----------------------------------------

def _task_state(version=2, **extra):
    return {"task_id": "t1", "instruction_version": version, "instruction_status": "ACTIVE", "supersessions": [],
            "requirement_supplements": [{"action": "write", "target": "external_resource", "necessity": "required",
                                         "source_span": "요약을 저장해", "target_ref": None, "condition": None,
                                         "instruction_version": 1}],
            "intended_target": {"path": "/ws/summary.md", "instruction_version": 1},
            "work_records": [{"request_id": "r1", "event_id": "e1", "instruction_version": 1,
                              "source_text": "회의 내용을 요약해서 저장해 줘",
                              "requirements": {"file_change:/ws/summary.md": "done"},
                              "artifacts": [{"path": "/ws/summary.md", "call": "w1", "kind": "created"}],
                              "results": [{"call": "w1", "tool": "write_file", "target": "/ws/summary.md",
                                           "outcome": "ok", "changed": True}],
                              "unresolved": [], "stop_reason": "completed"}], **extra}


def _followup(relation="continue", state=None):
    from nanobot.agent.context_selection import ContextTrace, SelectedContext
    sel = SelectedContext(trace=ContextTrace(request_id="r2"))
    sel.design = SimpleNamespace(task_id="t1", task_relation=relation, unresolved=[])
    sel.state = state or _task_state()
    sel.current_event = SimpleNamespace(content="그 문서를 줄여줘", event_id="e2")
    sel.artifacts = [{"path": "other.md", "full_path": "/ws/other.md", "kind": "created", "event_id": "x"}]
    return sel


def test_followup_restores_linked_artifact_but_not_old_obligations():
    from nanobot.agent.context_selection import prior_work, restore_task_work
    sel = _followup()
    restore_task_work(sel)
    assert any(a.get("linked_task") == "t1" and a["full_path"] == "/ws/summary.md" for a in sel.artifacts)
    assert sel.requirement_supplements == [] and sel.intended_target is None   # older version: history only
    view = prior_work(sel)
    assert view[0]["completed_do_not_redo"] == ["file_change:/ws/summary.md"] and view[0]["status"] == "earlier_turn"


def test_new_topic_gets_nothing_attached():
    from nanobot.agent.context_selection import prior_work, restore_task_work
    sel = _followup(relation="new")
    restore_task_work(sel)
    assert prior_work(sel) == [] and not any(a.get("linked_task") for a in sel.artifacts)


def test_same_request_resumed_restores_active_state():
    from nanobot.agent.context_selection import restore_task_work
    state = _task_state(version=1)
    state["execution_plan"] = {"version": 2, "facts": [], "failures": [], "no_progress": [], "instruction_version": 1}
    sel = _followup(state=state)
    restore_task_work(sel)
    assert sel.requirement_supplements and sel.intended_target["path"] == "/ws/summary.md"
    assert sel.exec_plan["version"] == 2


def test_cancelled_or_superseded_turns_are_history():
    from nanobot.agent.context_selection import prior_work
    state = _task_state(supersessions=[{"operation": "replace", "instruction_version": 2}])
    state["work_records"][0]["requirements"] = {"file_change:/ws/summary.md": "open"}
    view = prior_work(_followup(state=state))
    assert view[0]["status"] == "superseded_history" and "history_only" in view[0]
    state = _task_state(instruction_status="CANCELLED")
    assert prior_work(_followup(state=state))[0]["status"] == "cancelled_history"


def test_deictic_reference_binds_to_the_linked_task_output():
    text = "그 문서를 수정해 줘"
    pool = [{"path": "summary.md", "full_path": "/ws/summary.md", "kind": "created", "linked_task": "t1"},
            {"path": "other.md", "full_path": "/ws/other.md", "kind": "created"}]
    rels = parse_relations(text)
    bind_targets(rels, [], pool, text, "/ws")
    rel = next(r for r in rels if r.target == "external_resource")
    assert rel.bound_id == "/ws/summary.md"
    rels = parse_relations(text)
    bind_targets(rels, [], [dict(p, linked_task=None) for p in pool], text, "/ws")
    rel = next(r for r in rels if r.target == "external_resource")
    assert rel.bound_id is None and rel.candidates                      # no link: still asked


# ---- item 5: cause hypotheses ---------------------------------------------------------------------------

def test_model_cause_never_overwrites_the_recorded_failure():
    sel = FakeSel(versions={"/ws/a.md": "v1"})
    cp.record_fact(sel, call("f", "web_fetch", {"url": "https://x"}), rec(status="error", failure="fetch_rejected"))
    out = cp.update_plan(sel, {"next_action": "other_method", "cause": "unsuitable_approach", "failed_call": "f",
                               "change_reason": "다른 출처", "method": "official site"})
    failure = sel.exec_plan["failures"][0]
    assert isinstance(out, dict) and failure["cause"] is None and failure["error_kind"] == "fetch_rejected"
    assert failure["cause_hypothesis"]["status"] == "unconfirmed"


def test_unconfirmed_hypothesis_adds_no_block():
    sel = FakeSel()
    cp.record_fact(sel, call("s", "web_search", {"query": "q"}), rec(status="error", failure="no_results"))
    cp.update_plan(sel, {"next_action": "extra_lookup", "cause": "info_shortage", "failed_call": "s"})
    assert cp.blocked_repeat(sel, "web_search", {"query": "other q"}) is None


# ---- item 6: update_plan ---------------------------------------------------------------------------------

def test_only_changed_fields_are_needed_and_others_are_kept():
    sel = FakeSel()
    sel.exec_plan.update(cp.plan_of(sel), shape={"method": "m1", "order": ["a", "b"], "depends_on": []},
                         next_action="continue")
    out = cp.update_plan(sel, {"step": "b"})
    assert isinstance(out, dict) and sel.exec_plan["shape"]["order"] == ["a", "b"]
    assert sel.exec_plan["next_action"] == "continue" and "method" in out["kept_fields"]
    out = cp.update_plan(sel, {"next_action": "other_method", "cause": "unsuitable_approach", "failed_call": "x",
                               "change_reason": "r", "method": "m2"})
    assert isinstance(out, str)                                      # cites no attempt -> its own error only


def test_empty_update_is_no_change_not_an_error():
    sel = FakeSel()
    out = cp.update_plan(sel, {})
    assert isinstance(out, dict) and out["no_change"] is True
    assert isinstance(cp.update_plan(sel, None), dict)


def test_malformed_update_changes_nothing():
    sel = FakeSel()
    sel.state = {"assumptions": [{"text": "A"}]}
    out = cp.update_plan(sel, {"next_action": "revise_plan", "cause": "wrong_assumption", "assumption": "A",
                               "change_reason": "r", "method": "m"})          # no affected IDs
    assert isinstance(out, str) and sel.state["assumptions"] == [{"text": "A"}]
    assert sel.exec_plan["version"] == 1


@pytest.mark.asyncio
async def test_rejected_update_does_not_block_independent_action(env, tmp_path):
    from .test_context_t47_scenarios import META_for, calls, final, run, tool_text, two_files
    sel, text = await two_files(env, tmp_path)
    A = str(tmp_path / "t47_a.md")
    steps = [calls(("up", "revise_context_requirements", {"reason": "update_plan", "information_needs": [],
                    "completion_checks": [], "progress": {"next_action": "revise_plan"}}),     # malformed
                   ("wa", "write_file", {"path": A, "content": "우유 사기\n", **META_for(A)})),
             final("t47_a.md를 만들었습니다.")]
    _, result = await run(sel, steps, text)
    assert "cause" in tool_text(result.messages, "up")
    assert sel.observations["wa"]["status"] == "ok"                    # ran under its own checks
    assert result.stop_reason != "completed"                            # t47_b.md still required

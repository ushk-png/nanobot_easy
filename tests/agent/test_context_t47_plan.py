"""t47 unit checks of the plan state (MOCK designs, real code)."""
import json
from types import SimpleNamespace

import pytest

from nanobot.agent import context_plan as cp
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401


async def sel_for(env, text="t47_u.md 파일을 만들어 줘."):
    event(env, text)
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[]), request=text)
    return sel


def call(cid, name, **args):
    return SimpleNamespace(id=cid, name=name, arguments=args)


@pytest.mark.asyncio
async def test_update_plan_structure_is_verified(env):
    sel = await sel_for(env)
    assert "next_action" in cp.update_plan(sel, {"next_action": "jump"})
    assert "unknown requirement" in cp.update_plan(sel, {"next_action": "continue", "remaining": ["invented"]})
    assert "cause and a change_reason" in cp.update_plan(sel, {"next_action": "revise_plan"})
    assert "addressed by" in cp.update_plan(sel, {"next_action": "final_report", "cause": "wrong_assumption",
                                                  "change_reason": "x"})
    assert "transport failure" in cp.update_plan(sel, {"next_action": "continue", "cause": "transient_error",
                                                       "failed_call": "nope", "change_reason": "retry"})
    assert "active assumption" in cp.update_plan(sel, {"next_action": "revise_plan", "cause": "wrong_assumption",
                                                       "assumption": "made up", "change_reason": "x"})
    assert "record attempt errored" in cp.update_plan(sel, {"next_action": "final_report", "cause": "verifier_defect",
                                                            "check": "c", "change_reason": "x"})
    ok = cp.update_plan(sel, {"next_action": "continue", "obtained": "nothing yet"})
    assert ok["plan_version"] == 1 and sel.exec_plan["last_change"] is None    # no change, no new version


@pytest.mark.asyncio
async def test_no_progress_needs_same_requirement_target_args_result_and_version(env):
    sel = await sel_for(env)
    rec = {"status": "error", "read_only": True, "resource_id": "/x", "failure_class": "tool_error",
           "output_hash": "h1"}
    cp.record_fact(sel, call("a", "read_file", path="/x"), rec)
    assert not sel.exec_plan["no_progress"]
    cp.record_fact(sel, call("b", "read_file", path="/y"), rec)                 # other args: other attempt
    assert not sel.exec_plan["no_progress"]
    cp.record_fact(sel, call("c", "read_file", path="/x", intent_summary="again"), dict(rec, output_hash="h2"))
    assert not sel.exec_plan["no_progress"]                                     # different result
    cp.record_fact(sel, call("d", "read_file", path="/x"), rec)
    assert sel.exec_plan["no_progress"][0]["calls"] == ["a", "d"]
    assert cp.blocked_repeat(sel, "read_file", {"path": "/x"}).startswith("No progress")
    assert cp.blocked_repeat(sel, "read_file", {"path": "/z"}) is None          # other work not halted


@pytest.mark.asyncio
async def test_state_view_is_compact_and_bounded(env):
    sel = await sel_for(env)
    for i in range(60):
        cp.record_fact(sel, call(f"c{i}", "read_file", path=f"/p{i}"),
                       {"status": "error", "read_only": True, "resource_id": f"/p{i}", "failure_class": "tool_error",
                        "output_hash": str(i)})
    view = cp.state_view(sel)
    assert len(view["recent_results"]) == 4 and len(view["failed_attempts"]) <= 3
    assert len(json.dumps(view, ensure_ascii=False)) < 4000
    assert len(sel.exec_plan["facts"]) == 30                                   # record itself is bounded too

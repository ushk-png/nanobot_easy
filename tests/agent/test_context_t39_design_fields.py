"""t39 unit 3: first-design field consumers moved (request_type, completion_specs, obligations)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import nanobot.agent.context_selection as cs
from nanobot.agent.context_selection import (IntentDesign, RecordContextCompletionTool,
                                             ReviseContextRequirementsTool, direct_blockers)
from tests.agent.test_context_t37_museum import args, museum, resolve

TYPES = ["explain", "investigate", "compare", "modify", "execute", "other"]


def test_request_type_is_optional_routing_label():
    a = args("p2-museum-r1-t1"); a.pop("request_type")
    assert IntentDesign.model_validate(a).request_type == "other"


@pytest.mark.parametrize("label", ["p2-museum-r1-t1", "p2-file-r1-t1", "p2-perm-r1-t1"])
def test_request_type_changes_no_allowance_obligation_or_completion(monkeypatch, label):
    outs = set()
    for t in TYPES:
        sel, _ = museum(monkeypatch)
        sel.design = IntentDesign.model_validate({**args(label), "request_type": t})
        sel._relations = None
        sel.answer_text = "답"
        payload = {"request": sel.request_text, "sources": [], "capabilities": [], "task_candidates": []}
        outs.add((sel.answer_only(), sel.completion_supported(), tuple(sel.unmet_reasons()),
                  tuple(direct_blockers(sel.design, payload, {}))))
    assert len(outs) == 1


def test_first_design_rules_no_longer_carry_detailed_result_specs():
    assert "sum_parts" not in cs.DESIGN_RULES and "sum_parts" in cs.VERIFICATION_SPEC_RULES


def test_spec_declared_at_verification_and_never_replaced(monkeypatch):
    sel, tool = museum(monkeypatch)
    resolve(tool, "-41"); resolve(tool, "-42")
    rec = RecordContextCompletionTool(sel)
    check = "특별전과 구분한다."
    sel.design.completion_specs = [c for c in sel.design.completion_specs if c.check != check]
    cid = next(iter(sel.observations))
    asyncio.run(rec.execute(check=check, tool_call_ids=[cid], status="passed", finding="f",
                            spec={"result_kind": "answer_content"}))
    assert any(c.check == check and c.result_kind == "answer_content" for c in sel.design.completion_specs)
    out = asyncio.run(rec.execute(check=check, tool_call_ids=[cid], status="passed", finding="f",
                                  spec={"result_kind": "semantic"}))
    assert getattr(out, "is_error", False)


def test_plan_revision_cannot_remove_user_requirements(monkeypatch):
    sel, tool = museum(monkeypatch)
    before = (sel.user_requirements()["constraints"], [r.source_span for r in sel.user_requirements()["relations"]])
    asyncio.run(ReviseContextRequirementsTool(sel).execute(
        reason="release", information_needs=[], completion_checks=[],
        release=[{"need_id": "special_vs_permanent", "reason": "unnecessary"}]))
    after = (sel.user_requirements()["constraints"], [r.source_span for r in sel.user_requirements()["relations"]])
    assert before == after and "special_vs_permanent" in sel.model_plan()["information_needs"]

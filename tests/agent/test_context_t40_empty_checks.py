"""t40 unit 2: first-design completion_checks may be empty; empty != completable without verification;
wrong model specs are replaceable with evidence + history (user requirements are not specs)."""
import asyncio
from types import SimpleNamespace

import pytest

from nanobot.agent.context_selection import IntentDesign, RecordContextCompletionTool, validate_meaning
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401
from .test_context_t37_museum import args, museum, resolve


def test_empty_checks_validate():
    d = IntentDesign.model_validate({**args("p2-file-r1-t1"), "completion_checks": [], "completion_specs": []})
    assert d.completion_checks == []


@pytest.mark.asyncio
async def test_empty_checks_direct_answer_is_one_call_and_keeps_constraints(env):
    text = "드라이버는 빼고 60분 골프 연습 계획을 세워 줘"
    event(env, text)
    MOCK = plan(completion_checks=[], answer="퍼팅 30분, 아이언 30분")   # MOCK design
    _, provider, sel = await select(env, MOCK, request=text)
    assert len(provider.requests) == 1 and sel.blocked is None and sel.design.completion_checks == []
    assert sel.completion_supported()          # answer kind is checked by the answer itself


@pytest.mark.asyncio
async def test_empty_checks_for_work_are_not_completion_without_verification(env):
    text = "notes.md를 수정해 줘"
    event(env, text)
    _, provider, sel = await select(env, plan(route="execute", answer=None, completion_checks=[]), request=text)  # MOCK
    assert len(provider.requests) == 1 and sel.blocked is None      # empty list: no repair, no block
    target = [r for r in sel.required_results() if r["kind"] == "file_change"][0]["target"]
    sel.observations["w1"] = {"status": "ok", "read_only": False, "resource_id": target, "tool_call_id": "w1"}
    assert not sel.result_evidence_gaps() and sel.verification_unselected() and not sel.completion_supported()
    out, status = sel.verdict("수정했습니다.")
    assert status != "completed" and "verification_not_selected" in out
    sel.design.completion_checks.append("notes.md 내용이 바뀌었다")      # executor chooses the check later
    sel.completion_reports["notes.md 내용이 바뀌었다"] = {"status": "passed", "instruction_version": sel.instruction_version,
                                                     "tool_call_ids": ["w1"]}
    assert not sel.verification_unselected()


def test_wrong_model_spec_replaced_only_with_reason_and_evidence(monkeypatch):
    sel, tool = museum(monkeypatch)
    resolve(tool, "-41"); resolve(tool, "-42")
    rec = RecordContextCompletionTool(sel)
    check = sel.design.completion_checks[0]
    sel.design.completion_specs = [c for c in sel.design.completion_specs if c.check != check]
    cid = next(iter(sel.observations))
    run = lambda **k: asyncio.run(rec.execute(check=check, tool_call_ids=[cid], status="passed", finding="f", **k))
    run(spec={"result_kind": "semantic"})
    assert getattr(run(spec={"result_kind": "answer_content"}), "is_error", False)          # no reason
    run(spec={"result_kind": "answer_content"}, spec_revision_reason="the check is about the answer text")
    now = [c for c in sel.design.completion_specs if c.check == check]
    assert len(now) == 1 and now[0].result_kind == "answer_content"
    h = sel.spec_history[-1]
    assert h["old"][0]["result_kind"] == "semantic" and h["evidence"] == [cid] and h["reason"]
    assert sel.user_requirements()["constraints"] == [c.text for c in sel.design.constraints]

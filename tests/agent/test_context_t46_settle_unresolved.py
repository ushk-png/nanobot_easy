"""t46 unit 2: a default assumption in the design's `unresolved` list blocked a file creation (t41 E1-1).

REAL-RECORD REPLAY: the design is the real model output recorded in t41 (fixture, unchanged except the
constraint's source event id, which must point at this test's event). The settlement call itself is a
MOCK executor call (the real run never made one; the tool did not exist). Code verifies it.
OPPOSITE CASES: a missing target, a target/permission claimed as an assumption, a span not in the request,
and a content reference to material that is not there still block."""
import json
from pathlib import Path

import pytest

from nanobot.agent.context_selection import IntentDesign, ReviseContextRequirementsTool
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401

FX = json.loads((Path(__file__).parent / "fixtures" / "t41_e1_1_real.json").read_text())
ENTRY = FX["design"]["unresolved"][0]


def fs_tools(env):
    from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
    ws = Path(env[0].root) if hasattr(env[0], "root") else None
    reg = env[2]
    for t in (WriteFileTool(workspace=ws), ReadFileTool(workspace=ws)):
        if reg.get(t.name) is None:
            reg.register(t)


async def real_design(env):
    fs_tools(env)
    ev = event(env, FX["request"])
    data = json.loads(json.dumps(FX["design"]))
    for c in data["constraints"]:
        c["source_ids"] = [ev.event_id]
    _, _, sel = await select(env, IntentDesign.model_validate(data), request=FX["request"])
    return sel


async def settle(sel, **e):
    entry = {"text": ENTRY, "disposition": "acceptable_assumption", "aspect": "content",
             "basis_span": "오늘 할 일 세 개를 적어 줘", "basis": "delegated_content",
             "invents_facts": False} | e   # t47: basis + invents_facts required for an assumption
    return await ReviseContextRequirementsTool(sel).execute(reason="settle_unresolved", information_needs=[],
                                                            completion_checks=[], settle_unresolved=[entry])


def err(r):
    return getattr(r, "is_error", False)


@pytest.mark.asyncio
async def test_real_record_replay_reproduces_the_block_then_settles(env):
    sel = await real_design(env)
    # REAL-RECORD: the same gate result as recorded in t41
    gate = sel.mutation_gate("write_file")
    assert gate and gate.startswith(FX["mutation_gate"][0]["result"].split(" Classify")[0])
    assert [c["code"] for c in sel.unresolved_conditions()] == ["design_unresolved"]
    assert "settle_unresolved" in gate                              # the gate now names the way out
    # MOCK executor settlement, verified by code
    r = await settle(sel)
    assert not err(r), r
    assert sel.mutation_gate("write_file") is None
    assert sel.state["assumptions"][0]["disposition"] == "acceptable_assumption"
    assert sel.settled_unresolved[ENTRY]["instruction_version"] == sel.instruction_version
    # the real recorded write_file content now runs (file written here, observation as the runner records it)
    target = [r["target"] for r in sel.required_results() if r["kind"] == "file_change"][0]
    Path(target).write_text(FX["write_file_content"])
    sel.observations["w1"] = {"status": "ok", "read_only": False, "resource_id": target,
                              "result_kind": "mutation_result", "tool_call_id": "w1"}
    out, status = sel.verdict("t24_todo.md 를 만들었습니다.")
    # what remains is the real design's own after-change read-back need, not the assumption block
    assert status != "completed" and "verify_todo_file" in out and "design_unresolved" not in out


@pytest.mark.asyncio
async def test_settlement_does_not_survive_a_new_instruction_version(env):
    sel = await real_design(env)
    assert not err(await settle(sel))
    sel.settled_unresolved[ENTRY]["instruction_version"] = sel.instruction_version - 1
    assert sel.mutation_gate("write_file")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"aspect": "target"}, {"aspect": "permission"}, {"aspect": "evidence"},
    {"basis_span": "아무 내용이나 적어도 된다고 했다"},                    # not the user's words
    {"text": "다른 항목"},                                                # not an entry
    {"disposition": "already_resolved", "aspect": "permission"},
])
async def test_wrong_settlements_rejected_and_block_stays(env, bad):
    sel = await real_design(env)
    assert err(await settle(sel, **bad))
    assert sel.mutation_gate("write_file") and not sel.settled_unresolved


@pytest.mark.asyncio
async def test_required_from_user_keeps_blocking(env):
    sel = await real_design(env)
    r = await settle(sel, disposition="required_from_user")
    assert not err(r) and sel.mutation_gate("write_file")


@pytest.mark.asyncio
async def test_opposite_missing_target_cannot_be_assumed(env):
    text = "파일을 하나 만들어서 오늘 할 일 세 개를 적어 줘."                 # no file named
    fs_tools(env)
    ev = event(env, text)
    design = plan(route="execute", answer=None, completion_checks=[], capabilities_needed=["write_file"],
                  constraints=[{"text": text, "source_ids": [ev.event_id]}],
                  unresolved=["파일 이름이 지정되지 않았다. t24_todo.md로 만든다."])          # MOCK design
    _, _, sel = await select(env, design, request=text)
    # The target itself is never assumed (t46, kept).
    r = await ReviseContextRequirementsTool(sel).execute(
        reason="settle_unresolved", information_needs=[], completion_checks=[], settle_unresolved=[{
            "text": design.unresolved[0], "disposition": "acceptable_assumption", "aspect": "target",
            "basis_span": "파일을 하나 만들어서"}])
    assert err(r)
    assert sel.mutation_gate("write_file")
    # t47 change: the generic noun "파일" is not a target; a new output's path is decided within the
    # permitted scope and bound to the file actually created (no guessed path, no pre-existence needed).
    rel = next(r for r in sel.relations().relations if r.action == "write")
    assert rel.noun == "파일" and rel.target_ref is None and rel.new_artifact
    assert sel.required_results() == [{"kind": "file_change", "target": None}]


@pytest.mark.asyncio
async def test_opposite_content_the_user_referred_to_is_missing(env):
    """The user refers to content they gave 'earlier' and it is not there: the basis is not a delegation.
    A model that still calls it an assumption must cite the delegating words; the referenced clause is
    rejected while any other condition is open (here: no grounded change relation in the MOCK design),
    and required_from_user keeps the block."""
    text = "아까 말한 할 일 세 개를 t24_todo.md 파일에 적어 줘."
    fs_tools(env)
    ev = event(env, text)
    design = plan(route="execute", answer=None, completion_checks=[], capabilities_needed=["write_file"],
                  constraints=[{"text": text, "source_ids": [ev.event_id]}],
                  unresolved=["아까 말한 할 일 세 개가 대화에 없다."])                       # MOCK design
    _, _, sel = await select(env, design, request=text)
    r = await ReviseContextRequirementsTool(sel).execute(
        reason="settle_unresolved", information_needs=[], completion_checks=[], settle_unresolved=[{
            "text": design.unresolved[0], "disposition": "required_from_user", "aspect": "content",
            "basis_span": "아까 말한 할 일 세 개"}])
    assert not err(r) and sel.mutation_gate("write_file")
    other = [c["code"] for c in sel.unresolved_conditions() if c["code"] != "design_unresolved"]
    if other:   # code also sees the dangling reference: an assumption claim is rejected
        r = await ReviseContextRequirementsTool(sel).execute(
            reason="settle_unresolved", information_needs=[], completion_checks=[], settle_unresolved=[{
                "text": design.unresolved[0], "disposition": "acceptable_assumption", "aspect": "content",
                "basis_span": "할 일 세 개를 t24_todo.md 파일에 적어 줘"}])
        assert err(r) and sel.mutation_gate("write_file")
    print("other conditions:", other)

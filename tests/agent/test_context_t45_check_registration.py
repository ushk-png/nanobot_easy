"""t45 unit 1: verification_not_selected resolves when the executor registers a valid check at result
verification (empty first-design completion_checks), bound to task/requirement/target/instruction
version; wrong allowances stay rejected. All designs here are MOCK designs (no model call)."""
import asyncio

import pytest

from nanobot.agent.context_selection import RecordContextCompletionTool, ReviseContextRequirementsTool
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401

TEXT = "notes.md를 수정해 줘"
CHECK = "notes.md에 다섯 줄이 있다"


async def _work(env, tmp_path):
    event(env, TEXT)
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[]), request=TEXT)  # MOCK
    target = [r for r in sel.required_results() if r["kind"] == "file_change"][0]["target"]
    (tmp_path / "notes.md").write_text("a\nb\nc\nd\ne\n")
    sel.observations["w1"] = {"status": "ok", "read_only": False, "resource_id": target,
                              "result_kind": "file_content", "tool_call_id": "w1"}
    return sel, target


async def rec(sel, check=CHECK, ids=("w1",), status="passed", **k):
    return await (RecordContextCompletionTool(sel).execute(
        check=check, tool_call_ids=list(ids), status=status, finding="f", **k))


def err(r):
    return getattr(r, "is_error", False)


@pytest.mark.asyncio
async def test_not_selected_register_link_record_reevaluate(env, tmp_path):
    sel, target = await _work(env, tmp_path)
    out, status = sel.verdict("수정했습니다.")
    assert status != "completed" and "verification_not_selected" in out
    r = await RecordContextCompletionTool(sel).execute(check=CHECK, tool_call_ids=["w1"], status="passed",
        finding="5 lines", spec={"result_kind": "file_content", "target": "notes.md",
                                 "expected_values": {"contains": "a\nb"}})
    assert not err(r), r
    b = sel.check_bindings[CHECK]
    assert b["target"] == target and b["instruction_version"] == sel.instruction_version
    assert b["requirements"] == ["file_change"] and b["evidence"] == ["w1"]
    assert sel.completion_reports[CHECK]["tool_call_ids"] == ["w1"]
    assert not sel.verification_unselected() and sel.completion_supported()
    out, status = sel.verdict("notes.md를 수정했습니다.")
    assert status == "completed" and "verification_not_selected" not in out
    assert out.startswith("notes.md를 수정했습니다.")          # final answer is the work's answer


@pytest.mark.asyncio
async def test_bad_text_keys_rejected_with_accepted_keys_and_can_be_corrected(env, tmp_path):
    sel, _ = await _work(env, tmp_path)
    r = await rec(sel, spec={"result_kind": "file_content", "target": "notes.md", "expected_values": {"lines": 5}})
    assert err(r) and "accepted keys: content, contains, path" in str(r)
    assert CHECK not in sel.design.completion_checks and CHECK not in sel.check_bindings   # rolled back
    assert sel.verification_unselected()
    assert not err(await rec(sel, spec={"result_kind": "file_content", "target": "notes.md",
                                  "expected_values": {"contains": "e"}}))   # no spec_revision_reason needed


@pytest.mark.asyncio
async def test_wrong_allowances_still_rejected(env, tmp_path):
    sel, _ = await _work(env, tmp_path)
    (tmp_path / "other.md").write_text("x")
    spec = {"result_kind": "file_content", "target": "notes.md"}
    assert err(await rec(sel, spec=None))                                   # no spec
    assert err(await rec(sel, spec={"result_kind": "file_content"}))        # no target for a change
    assert err(await rec(sel, spec={"result_kind": "file_content", "target": "other.md"}))   # unrelated target
    assert err(await rec(sel, ids=["nope"], spec=spec))                     # no evidence
    sel.observations["f1"] = {"status": "error", "read_only": False, "tool_call_id": "f1"}
    assert err(await rec(sel, ids=["f1"], spec=spec))                       # failed evidence only
    assert err(await rec(sel, spec={**spec, "expected_values": {"contains": "zzz"}}))   # content not there
    assert sel.design.completion_checks == [] and sel.verification_unselected()
    out, status = sel.verdict("수정했습니다.")
    assert status != "completed"


@pytest.mark.asyncio
async def test_registration_needs_the_required_result_not_only_a_check(env, tmp_path):
    event(env, TEXT)
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[]), request=TEXT)  # MOCK
    sel.observations["r1"] = {"status": "ok", "read_only": True, "resource_id": "x", "tool_call_id": "r1"}
    r = await rec(sel, ids=["r1"], spec={"result_kind": "sourced", "target": "notes.md"})
    # even if a check passes on a read, the file change itself is still required for completion
    if not err(r):
        assert sel.result_evidence_gaps()
    out, status = sel.verdict("수정했습니다.")
    assert status != "completed"
    assert sel.user_requirements() == sel.user_requirements()  # unchanged shape
    assert [r["kind"] for r in sel.required_results()] == ["file_change"]


@pytest.mark.asyncio
async def test_duplicate_registration_blocked_and_stale_pass_not_counted(env, tmp_path):
    # t46: the t45 task-wide rule ("a passed check blocks any new one") is replaced by per-requirement
    # binding; the same requirement/target/kind twice is still rejected as a duplicate.
    sel, _ = await _work(env, tmp_path)
    assert not err(await rec(sel, spec={"result_kind": "file_content", "target": "notes.md"}))
    r = await rec(sel, check="another", spec={"result_kind": "file_content", "target": "notes.md"})
    assert err(r) and "duplicate check" in str(r) and "another" not in sel.design.completion_checks
    sel.completion_reports[CHECK]["instruction_version"] = sel.instruction_version - 1
    assert sel.verification_unselected()


@pytest.mark.asyncio
async def test_revise_binds_checks(env, tmp_path):
    sel, _ = await _work(env, tmp_path)
    await ReviseContextRequirementsTool(sel).execute(reason="missing_evidence", information_needs=[],
                                                     completion_checks=[CHECK])
    assert sel.check_bindings[CHECK]["registered_by"] == "revise_context_requirements"
    assert sel.check_bindings[CHECK]["instruction_version"] == sel.instruction_version


@pytest.mark.asyncio
async def test_mismatching_spec_on_design_check_is_not_loosened(env, tmp_path):
    sel, _ = await _work(env, tmp_path)
    await ReviseContextRequirementsTool(sel).execute(reason="missing_evidence", information_needs=[],
                                                     completion_checks=[CHECK])
    strict = {"result_kind": "file_content", "target": "notes.md", "expected_values": {"contains": "zzz"}}
    assert err(await rec(sel, spec=strict))
    assert err(await rec(sel, spec={"result_kind": "file_content", "target": "notes.md"}))   # no loosening
    bad = await rec(sel, check="c2", spec={"result_kind": "file_content", "target": "notes.md"})
    assert not err(bad) or "validated design" in str(bad)


@pytest.mark.asyncio
async def test_e3_2_shape_revise_then_bad_keys_then_corrected(env, tmp_path):
    """Replay of the t41 E3-2 call shape (MOCK model, real tool code): revise adds the check, the first
    record uses unrecognized text keys, the corrected record passes and the turn completes."""
    sel, _ = await _work(env, tmp_path)
    assert err(await rec(sel))                                         # not in design yet -> no spec
    await ReviseContextRequirementsTool(sel).execute(reason="missing_evidence", information_needs=[],
                                                     completion_checks=[CHECK])
    r = await rec(sel, spec={"result_kind": "file_content", "target": "notes.md",
                             "expected_values": {"heading": "x", "lines": 5}})
    assert err(r) and "accepted keys" in str(r)
    assert not err(await rec(sel, spec={"result_kind": "file_content", "target": "notes.md",
                                        "expected_values": {"contains": "a\nb\nc"}}))
    out, status = sel.verdict("notes.md를 수정했습니다.")
    assert status == "completed"


@pytest.mark.asyncio
async def test_e2_1_shape_two_files_one_check_per_target(env, tmp_path):
    """t41 E2-1 call shape (MOCK model): a combined target "a, b" is not one resource; per-target
    checks register and complete only when every required file has its change."""
    text = "t24_a.md 파일과 t24_b.md 파일을 만들어서 각각 할 일 하나씩 적어 줘."
    event(env, text)
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[]), request=text)  # MOCK
    targets = [r["target"] for r in sel.required_results() if r["kind"] == "file_change"]
    if not targets or None in targets:
        pytest.skip("mock relations did not ground a target")
    for i, (name, body) in enumerate((("t24_a.md", "회의 자료 정리"), ("t24_b.md", "메일 회신"))):
        (tmp_path / name).write_text("- [ ] " + body + "\n")
        sel.observations[f"w{i}"] = {"status": "ok", "read_only": False, "resource_id": str(tmp_path / name),
                                     "result_kind": "file_content", "tool_call_id": f"w{i}"}
    r = await rec(sel, check="both", ids=["w0", "w1"], spec={"result_kind": "file_content",
        "target": "t24_a.md, t24_b.md", "expected_values": {"t24_a.md": "회의 자료 정리"}})
    assert err(r) and "one check per target" in str(r)
    assert not err(await rec(sel, check="a", ids=["w0"], spec={"result_kind": "file_content", "target": "t24_a.md",
                                                                "expected_values": {"contains": "회의 자료 정리"}}))
    out, status = sel.verdict("두 파일을 만들었습니다.")
    assert status == "completed", out

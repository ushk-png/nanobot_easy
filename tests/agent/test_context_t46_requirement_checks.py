"""t46 unit 1 (MOCK designs, real tool code): check registration is per requirement, not per task.
Each check is bound to requirement/target/kind/instruction version/target version and phase."""
import pytest

from nanobot.agent.context_selection import RecordContextCompletionTool
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401


def err(r):
    return getattr(r, "is_error", False)


async def rec(sel, check, ids, spec=None, status="passed"):
    k = {"spec": spec} if spec else {}
    return await RecordContextCompletionTool(sel).execute(check=check, tool_call_ids=list(ids), status=status,
                                                          finding="f", **k)


def write(sel, tmp_path, cid, name, body, read_only=False):
    (tmp_path / name).write_text(body) if not read_only else None
    sel.observations[cid] = {"status": "ok", "read_only": read_only, "resource_id": str(tmp_path / name),
                             "result_kind": "mutation_result" if not read_only else "read_result",
                             "tool_call_id": cid}


async def one_file(env, tmp_path):
    text = "t24_todo.md 파일을 만들어서 '- [ ] 장보기'를 적어 줘."
    event(env, text)
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[]), request=text)
    targets = [r["target"] for r in sel.required_results() if r["kind"] == "file_change"]
    if not targets or None in targets:
        pytest.skip("mock relations did not ground a target")
    return sel


@pytest.mark.asyncio
async def test_existence_pass_then_content_check_added(env, tmp_path):
    sel = await one_file(env, tmp_path)
    write(sel, tmp_path, "w1", "t24_todo.md", "- [ ] 장보기\n")
    assert not err(await rec(sel, "exists", ["w1"], {"result_kind": "mutation_result", "target": "t24_todo.md"}))
    r = await rec(sel, "content", ["w1"], {"result_kind": "mutation_result", "target": "t24_todo.md",
                                            "expected_values": {"contains": "장보기"}})
    # same kind + target is a duplicate; content is checked as file_content on a read-back
    assert err(r) and "duplicate check" in str(r)
    sel.observations["r1"] = {"status": "ok", "read_only": True, "resource_id": str(tmp_path / "t24_todo.md"),
                              "result_kind": "file_content", "tool_call_id": "r1"}
    assert not err(await rec(sel, "content", ["r1"], {"result_kind": "file_content", "target": "t24_todo.md",
                                                       "expected_values": {"contains": "장보기"}}))
    assert sel.completion_reports["exists"]["status"] == "passed"          # earlier pass preserved
    b = sel.check_bindings["content"]
    assert b["requirement"].startswith("file_change:") and b["kind"] == "file_content"
    assert b["phase"] == "post_execution" and b["instruction_version"] == sel.instruction_version
    assert sel.completion_reports["content"]["target_version"] == sel.target_version(b["target"])
    out, status = sel.verdict("t24_todo.md 를 만들었습니다.")
    assert status == "completed", out


@pytest.mark.asyncio
async def test_multiple_files_one_passes_then_others(env, tmp_path):
    text = "t24_a.md 파일과 t24_b.md 파일을 만들어서 각각 할 일 하나씩 적어 줘."
    event(env, text)
    from nanobot.agent.context_selection import Obligations, RelationSpec
    rels = [RelationSpec(action="write", target="external_resource", necessity="required",
                         source_span=span, target_ref=ref)
            for span, ref in (("t24_a.md 파일과", "t24_a.md"), ("t24_b.md 파일을 만들어서", "t24_b.md"))]
    design = plan(route="execute", answer=None, completion_checks=[],
                  obligations=Obligations(target="external_resource", external_change="required", relations=rels))
    _, _, sel = await select(env, design, request=text)                                 # MOCK design
    assert len([r for r in sel.required_results() if r["kind"] == "file_change"]) == 2, sel.required_results()
    write(sel, tmp_path, "w0", "t24_a.md", "- [ ] 회의\n")
    assert not err(await rec(sel, "a", ["w0"], {"result_kind": "mutation_result", "target": "t24_a.md"}))
    write(sel, tmp_path, "w1", "t24_b.md", "- [ ] 메일\n")
    assert not err(await rec(sel, "b", ["w1"], {"result_kind": "mutation_result", "target": "t24_b.md"}))
    assert {sel.check_bindings[c]["requirement"].rsplit("/", 1)[-1] for c in ("a", "b")} == {"t24_a.md", "t24_b.md"}
    assert sel.completion_reports["a"]["status"] == "passed"
    out, status = sel.verdict("두 파일을 만들었습니다.")
    assert status == "completed", out


@pytest.mark.asyncio
async def test_unrelated_target_and_evidenceless_pass_rejected(env, tmp_path):
    sel = await one_file(env, tmp_path)
    write(sel, tmp_path, "w1", "t24_todo.md", "- [ ] 장보기\n")
    write(sel, tmp_path, "x1", "other.md", "x")
    assert err(await rec(sel, "other", ["x1"], {"result_kind": "mutation_result", "target": "other.md"}))
    assert err(await rec(sel, "noev", ["missing"], {"result_kind": "mutation_result", "target": "t24_todo.md"}))
    sel.observations["f1"] = {"status": "error", "read_only": False, "tool_call_id": "f1"}
    assert err(await rec(sel, "failed", ["f1"], {"result_kind": "mutation_result", "target": "t24_todo.md"}))
    assert sel.design.completion_checks == [] and sel.check_bindings == {}


@pytest.mark.asyncio
async def test_target_changed_after_pass_makes_it_stale(env, tmp_path):
    sel = await one_file(env, tmp_path)
    write(sel, tmp_path, "w1", "t24_todo.md", "- [ ] 장보기\n")
    assert not err(await rec(sel, "c", ["w1"], {"result_kind": "mutation_result", "target": "t24_todo.md"}))
    assert sel.check_passed("c")[0]
    (tmp_path / "t24_todo.md").write_text("changed\n")                      # content changed afterwards
    assert sel.check_stale("c") and sel.check_passed("c") == (False, [], "stale_target_changed")
    assert sel.verification_unselected()
    out, status = sel.verdict("만들었습니다.")
    assert status != "completed"
    # evidence predating the latest change cannot re-pass; a new write (or read after it) can
    write(sel, tmp_path, "w2", "t24_todo.md", "- [ ] 장보기\n")
    assert err(await rec(sel, "c", ["w1"]))
    assert not err(await rec(sel, "c", ["w2"])) and not sel.check_stale("c")
    # identical bytes rewritten: content version unchanged, still valid
    (tmp_path / "t24_todo.md").write_text("- [ ] 장보기\n")
    assert not sel.check_stale("c")


@pytest.mark.asyncio
async def test_pre_execution_check_not_invalidated_by_its_own_change(env, tmp_path):
    sel = await one_file(env, tmp_path)
    sel.observations["r0"] = {"status": "ok", "read_only": True, "resource_id": str(tmp_path / "t24_todo.md"),
                              "result_kind": "read_result", "tool_call_id": "r0"}
    assert not err(await rec(sel, "absent before", ["r0"], {"result_kind": "semantic", "target": "t24_todo.md"}))
    assert sel.check_bindings["absent before"]["phase"] == "pre_execution"
    assert sel.check_bindings["absent before"]["target_version_at_registration"] == "absent"
    write(sel, tmp_path, "w1", "t24_todo.md", "- [ ] 장보기\n")
    assert not sel.check_stale("absent before")
    # a pre-execution pass does not stand in for the post-execution result check
    assert not err(await rec(sel, "written", ["w1"], {"result_kind": "mutation_result", "target": "t24_todo.md"}))
    assert sel.check_bindings["written"]["phase"] == "post_execution"


@pytest.mark.asyncio
async def test_failed_registration_does_not_pollute_later_ones(env, tmp_path):
    sel = await one_file(env, tmp_path)
    write(sel, tmp_path, "w1", "t24_todo.md", "- [ ] 장보기\n")
    unfinished_before = list((sel.state or {}).get("unfinished", []))
    r = await rec(sel, "c", ["w1"], {"result_kind": "mutation_result", "target": "t24_todo.md",
                                      "expected_values": {"lines": 1}})
    assert err(r)
    assert "c" not in sel.design.completion_checks and "c" not in sel.check_bindings
    assert not [s for s in sel.design.completion_specs if s.check == "c"]
    assert list((sel.state or {}).get("unfinished", [])) == unfinished_before
    assert sel.trace.counters.get("verification_checks_rolled_back") == 1
    # the same requirement/kind registers cleanly afterwards (no phantom duplicate)
    assert not err(await rec(sel, "c2", ["w1"], {"result_kind": "mutation_result", "target": "t24_todo.md",
                                                  "expected_values": {"contains": "장보기"}}))
    assert sel.trace.counters["verification_checks"] == 1 and "registered_at_verification" in sel.trace.reasons

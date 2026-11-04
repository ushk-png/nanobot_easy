"""t40 unit 4: commit verification links tool execution, commit ID, target repo and requested change.
Temp git repos; model designs are MOCK; git commands are real."""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.context_evidence import observation
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401
from .test_context_t39_permission import _control

TEXT = "이 변경을 커밋해 줘"


def git(ws, *a):
    return subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(env):
    ws = env[1].workspace_ids[0]
    git(ws, "init", "-q", "-b", "main"); git(ws, "config", "user.email", "t@t"); git(ws, "config", "user.name", "t")
    git(ws, "add", "-A"); git(ws, "commit", "-qm", "init")
    git(ws, "checkout", "-qb", "other"); Path(ws, "o.txt").write_text("o"); git(ws, "add", "o.txt")
    git(ws, "commit", "-qm", "other"); git(ws, "checkout", "-q", "main")
    Path(ws, "x.txt").write_text("requested change")            # the change the user means
    return ws


async def _sel(env):
    event(env, TEXT)
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[]), request=TEXT)  # MOCK
    return sel


def run_tool(sel, cid, out):
    """Record a successful state-changing exec call with its real output (as the runtime does)."""
    call = SimpleNamespace(id=cid, name="exec", arguments={"command": "git ..."})
    sel.observations[cid] = observation(sel, call, out, "ok", SimpleNamespace(read_only=False))


def gap(sel):
    return [g for g in sel.result_evidence_gaps() if "repo_commit" in g]


@pytest.mark.asyncio
async def test_answer_only_does_not_pass(env, repo):
    sel = await _sel(env)
    assert gap(sel) == ["required_result_missing:repo_commit:no_new_commit"]
    out, status = sel.verdict("커밋했습니다.")
    assert status != "completed"


@pytest.mark.asyncio
async def test_branch_switch_only_does_not_pass(env, repo):
    sel = await _sel(env)
    run_tool(sel, "c1", subprocess.run(["git", "-C", repo, "checkout", "other"], capture_output=True, text=True).stderr)
    assert git(repo, "rev-parse", "HEAD") != sel.repo_head_start            # HEAD moved ...
    assert gap(sel) == ["required_result_missing:repo_commit:no_new_commit"]   # ... but no new commit


@pytest.mark.asyncio
async def test_other_change_committed_does_not_pass(env, repo):
    sel = await _sel(env)
    Path(repo, "y.txt").write_text("unrelated"); git(repo, "add", "y.txt")
    run_tool(sel, "c1", git(repo, "commit", "-m", "y only"))
    assert gap(sel) == ["required_result_missing:repo_commit:requested_change_not_committed"]


@pytest.mark.asyncio
async def test_commit_not_made_by_a_tool_call_does_not_pass(env, repo):
    sel = await _sel(env)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "outside")             # no tool output links it
    assert gap(sel) == ["required_result_missing:repo_commit:commit_not_linked_to_tool_call"]


@pytest.mark.asyncio
async def test_linked_commit_with_requested_change_passes_and_is_reported(env, repo):
    sel = await _sel(env)
    git(repo, "add", "-A")
    run_tool(sel, "c1", git(repo, "commit", "-m", "x"))
    assert gap(sel) == []
    head = git(repo, "rev-parse", "--short", "HEAD").strip()
    assert any(r.startswith(head) or head.startswith(r) for r in sel.observations["c1"]["object_refs"])
    sel.control.execution(SimpleNamespace(id="c1", name="exec", arguments={}), "ok")   # runtime execution record
    out, status = sel.verdict("커밋했습니다.")   # checks were never chosen -> not complete, change still reported
    assert status != "completed" and "verification_not_selected" in out and "실행된 변경: exec" in out


def test_stale_instruction_blocks_change_before_execution(tmp_path):
    c = _control(tmp_path, "notes.md 파일을 수정해 줘.")
    ran = []

    async def op():
        ran.append(1)
        return "ok", {"status": "ok"}, None

    async def drain_new():                 # a newer user instruction arrived after preparation
        return [SimpleNamespace(content="아니, 수정하지 마")]
    call = SimpleNamespace(id="c1", name="write_file", arguments={"path": str(tmp_path / "notes.md")})
    _, ev, _ = asyncio.run(c.run_mutation(call, SimpleNamespace(), op, drain_new))
    assert ev["status"] == "blocked" and ran == []
    c.selected.state["instruction_status"] = "RECONCILE"
    async def drain():
        return []
    _, ev, _ = asyncio.run(c.run_mutation(SimpleNamespace(id="c2", name="write_file",
                                          arguments={"path": str(tmp_path / "notes.md")}), SimpleNamespace(), op, drain))
    assert ev["status"] == "blocked" and ran == []

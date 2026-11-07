"""t39 unit 4: required result vs actual evidence replaces the _ACTION_REQUEST direct-answer block.

Integration in a temporary git repo. Model semantic outputs (the design proposals) are MOCK.
"""
import subprocess

import pytest

import nanobot.agent.context_selection as cs
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401


def git(ws, *a):
    return subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(env):
    ws = env[1].workspace_ids[0]
    git(ws, "init", "-q"); git(ws, "config", "user.email", "t@t"); git(ws, "config", "user.name", "t")
    git(ws, "add", "-A"); git(ws, "commit", "-qm", "init")
    return ws


def test_action_word_regex_is_gone():
    assert not hasattr(cs, "_ACTION_REQUEST") and not hasattr(cs, "unaccounted_action")


@pytest.mark.asyncio
async def test_explain_commit_is_answered(env, repo):
    text = "커밋 방법을 설명해 줘"
    event(env, text)
    MOCK = plan(answer="git add 후 git commit -m 으로 커밋합니다.")   # MOCK model design
    _, _, sel = await select(env, MOCK, request=text)
    assert sel.design.route == "direct" and sel.required_results() == [{"kind": "answer", "target": None}]
    assert sel.completion_supported()
    _, status = sel.verdict(MOCK.answer)
    assert status == "completed"


@pytest.mark.asyncio
async def test_editing_a_quoted_commit_sentence_is_an_answer_edit(env, repo):
    text = "'커밋해 줘'라는 문장을 고쳐 줘"
    event(env, text)
    MOCK = plan(request_type="modify", answer="'커밋해 주세요'")   # MOCK model design
    _, _, sel = await select(env, MOCK, request=text)
    assert [r["kind"] for r in sel.required_results()] == ["answer"]
    head = git(repo, "rev-parse", "HEAD")
    assert sel.completion_supported() and git(repo, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_commit_request_needs_an_actual_commit_not_an_answer(env, repo):
    text = "이 변경을 커밋해 줘"
    event(env, text)
    MOCK = plan(request_type="explain", answer="커밋했습니다.")   # MOCK model design claiming done
    _, _, sel = await select(env, MOCK, request=text)
    assert sel.design.route != "clarify"            # a clear work request is not sent to confirmation-wait
    assert sel.required_results() == [{"kind": "repo_commit", "target": repo}]
    assert not sel.answer_only() and not sel.completion_supported()   # the direct answer cannot bypass
    out, status = sel.verdict("커밋했습니다.")
    assert status != "completed" and "required_result_missing:repo_commit" in out
    (sel_ws := __import__("pathlib").Path(repo) / "x.txt").write_text("x")
    git(repo, "add", "-A")
    from types import SimpleNamespace as NS
    from nanobot.agent.context_evidence import observation
    out = git(repo, "commit", "-m", "change")                         # the actual commit, via a tool call (t40)
    sel.observations["c1"] = observation(sel, NS(id="c1", name="exec", arguments={}), out, "ok", NS(read_only=False))
    assert "required_result_missing:repo_commit" not in " ".join(sel.result_evidence_gaps())


@pytest.mark.asyncio
async def test_file_change_needs_a_change_on_that_target(env, repo):
    text = "notes.md를 수정해 줘"
    event(env, text)
    _, _, sel = await select(env, plan(answer="수정했습니다."), request=text)   # MOCK model design
    want = [r for r in sel.required_results() if r["kind"] == "file_change"]
    assert want and want[0]["target"].endswith("notes.md")
    sel.observations["c1"] = {"status": "ok", "read_only": False, "resource_id": want[0]["target"] + ".other"}
    assert sel.result_evidence_gaps()
    sel.observations["c2"] = {"status": "ok", "read_only": False, "resource_id": want[0]["target"]}
    assert not [g for g in sel.result_evidence_gaps() if "file_change" in g]

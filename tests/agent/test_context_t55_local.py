"""t55 local verification of execution control: explicit call/time caps, no-progress repeats of
successful actions, the real final verify -> fix -> return path under the shared budget, and whether a
local shell process started by the exec tool really ended. MOCK model, real AgentLoop / selector /
runner / provider gate / exec tool. Not a measure of real-model completion, budgets or token savings."""
import asyncio
import os
import subprocess
import time
import uuid

import pytest

from .t51_harness import Session, calls, design, final
from .test_context_selection import env as env  # noqa: F401
from .test_context_t51_local import A2, A2_WRONG, CHK2, meta, minutes, spec, write

SETUP = "unexpected executor call"
GOLF = "총 45분 골프 연습 계획: 퍼팅 20분, 나머지 어프로치·아이언 반씩, 드라이버 제외"


class SetupError(AssertionError):
    pass


def no_setup_error(out):
    """An exhausted MOCK queue is a test-construction error, counted apart from product results."""
    if out and SETUP in out:
        raise SetupError("TEST SETUP ERROR (mock response not prepared): " + out[-200:])


def explicit_session(tmp_path, monkeypatch, attempts, deadline):
    """t55 test entry point: explicit finite caps only (production defaults untouched)."""
    try:
        from nanobot.agent.task_budget import require_explicit_caps
    except ImportError:                          # baseline run on e08d7ac: same rule, test-local
        def require_explicit_caps(c):
            a, d = c.task_generation_attempts, c.task_deadline_seconds
            if not (isinstance(a, int) and a > 0 and isinstance(d, (int, float)) and d > 0):
                raise ValueError("explicit finite call and time caps required")
    s = Session(tmp_path, monkeypatch)
    base = s.loop.context_selection_config
    cfg = base.model_copy(update={"task_generation_attempts": attempts, "task_deadline_seconds": deadline})
    require_explicit_caps(cfg)                  # refuses before any run when caps are missing/invalid
    s.loop.context_selection_config = cfg
    return s


def sleepy(s):
    """MOCK steps may be ("sleep", seconds, response): the model call waits before answering."""
    plain = s.mock.chat

    async def chat(messages, **kw):
        tools = kw.get("tools") or []
        is_design = any((x.get("function") or {}).get("name") == "submit_context_design" for x in tools)
        queue = s.mock.steps
        if not is_design and queue and isinstance(queue[0], tuple) and queue[0][0] == "sleep":
            _, secs, resp = queue.pop(0)
            s.mock.requests.append({"design": False, "messages": messages, "tools": kw.get("tools")})
            await asyncio.sleep(secs)
            return resp
        return await plain(messages, **kw)
    s.mock.chat = chat


def exe(**kw):
    return design(goal="작업", route="execute", request_type="execute",
                  capabilities_needed=["read_file", "write_file", "exec"], **kw)


def rd(cid, path):
    return calls((cid, "read_file", {"path": str(path)}))


def budget_of(s):
    return s.selected[-1].trace.budget


def golf_design():
    return design(goal="45분 계획", route="execute", request_type="other",
                  **spec(CHK2, {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, 45))


# ==== 1. explicit budget and its start record ===============================================================
def test_entry_point_refuses_missing_or_invalid_caps(tmp_path, monkeypatch):
    for attempts, deadline in [(None, None), (6, None), (None, 30), (0, 30), (6, 0), (6, -1)]:
        with pytest.raises(ValueError):
            explicit_session(tmp_path, monkeypatch, attempts, deadline)


@pytest.mark.asyncio
async def test_start_record_explicit_caps(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 6, 30)
    no_setup_error(await s.turn("q", design(answer="답"), steps=[]))
    rec = budget_of(s).start_record()
    assert rec["call_cap"] == 6 and rec["call_cap_source"] == "explicit"
    assert rec["time_cap_s"] == 30 and rec["time_cap_source"] == "explicit" and rec["deadline_applied"] is True
    assert rec["token_cap"] == "unset"
    assert s.tasks()[-1]["run_budget"]["start"]["call_cap"] == 6


@pytest.mark.asyncio
async def test_start_record_default_reports_no_whole_deadline(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)                       # existing user config: not modified
    no_setup_error(await s.turn("q", design(answer="답"), steps=[]))
    rec = budget_of(s).start_record()
    assert rec["call_cap_source"].startswith("default") and rec["call_cap"] == s.loop.max_iterations + 4
    assert rec["deadline_applied"] is False and rec["time_cap"] == "전체 마감 없음"
    assert s.loop.context_selection_config.task_deadline_seconds is None


@pytest.mark.asyncio
async def test_supplement_and_plan_change_do_not_reset_consumption(tmp_path, monkeypatch):
    from .test_context_t52_local import sup
    s = explicit_session(tmp_path, monkeypatch, 4, 30)
    a = tmp_path / "a.md"
    a.write_text("자료\n")
    out = await s.turn("a.md를 읽고 정리해 주세요", exe(), steps=[
        rd("r1", a), calls(("sp", "revise_context_requirements", sup())), rd("r2", tmp_path / "b.md"),
        rd("r3", a), final("정리")])
    no_setup_error(out)
    b = budget_of(s)
    assert b.sends == len(s.mock.requests) == 4 and b.stop_reason == "budget_sends"
    assert "budget_sends" in out


@pytest.mark.asyncio
async def test_no_new_model_or_tool_work_after_deadline(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 20, 1.0)
    sleepy(s)
    f = tmp_path / "late.md"
    out = await s.turn("작업", exe(), steps=[("sleep", 5, calls(write("w", f, "late\n"))), final("끝")])
    no_setup_error(out)
    assert len(s.executor_requests()) == 1 and not f.exists()   # the late tool call never ran
    assert "budget_deadline" in out


# ==== 2. no-progress repeats of SUCCESSFUL actions ==========================================================
@pytest.mark.asyncio
async def test_same_page_read_successfully_with_same_result_stops(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 40, 60)
    p = tmp_path / "page.md"
    p.write_text("같은 내용\n")
    notations = [str(p), str(p).replace(str(tmp_path), str(tmp_path) + "/.")]   # cosmetic re-spelling
    out = await s.turn("page.md를 읽어 주세요", exe(),
                       steps=[rd("r%d" % i, notations[i % 2]) for i in range(30)])
    no_setup_error(out)
    assert len(s.executor_requests()) < 10 and "no_progress_repeat" in out
    rec = s.tasks()[-1]["run_budget"]
    assert rec["stop_reason"] == "no_progress_repeat"
    assert rec["last_progress"]["call"] == "r0"               # the first read was the last real progress


@pytest.mark.asyncio
async def test_plan_a_b_a_successful_reads_without_change_stop(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 40, 60)
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("A\n"); b.write_text("B\n")
    out = await s.turn("두 파일", exe(), steps=[rd("x%d" % i, a if i % 2 == 0 else b) for i in range(30)])
    no_setup_error(out)
    assert len(s.executor_requests()) < 14 and "no_progress_repeat" in out


@pytest.mark.asyncio
async def test_other_requirement_success_does_not_clear_repeat_record(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 40, 60)
    x = tmp_path / "x.md"
    x.write_text("X\n")
    steps = []
    for i in range(10):
        (tmp_path / ("n%d.md" % i)).write_text("새 자료 %d\n" % i)
        steps += [rd("x%d" % i, x), rd("n%d" % i, tmp_path / ("n%d.md" % i))]
    out = await s.turn("작업", exe(), steps=steps)
    no_setup_error(out)
    plan = s.selected[-1].exec_plan
    assert any(e["target"] == str(x) for e in plan["no_progress"])
    assert "no_progress_repeat" in out


@pytest.mark.asyncio
async def test_sequential_different_files_is_real_progress(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 20, 60)
    files = [tmp_path / ("f%d.md" % i) for i in range(8)]
    for i, f in enumerate(files):
        f.write_text("내용 %d\n" % i)
    out = await s.turn("8개 파일 읽기", exe(), steps=[*(rd("r%d" % i, f) for i, f in enumerate(files)),
                                                       final("8개 파일을 읽었습니다.")])
    no_setup_error(out)
    assert "no_progress_repeat" not in out and budget_of(s).stop_reason is None
    assert not s.selected[-1].exec_plan["no_progress"]


@pytest.mark.asyncio
async def test_reread_after_target_content_changed_is_allowed(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 20, 60)
    x = tmp_path / "x.md"
    x.write_text("v1\n")

    def change_then_read(messages):
        x.write_text("v2\n")                                   # the target changed outside
        return rd("r3", x)
    out = await s.turn("x.md 확인", exe(), steps=[rd("r1", x), rd("r2", x), change_then_read,
                                                 final("x.md는 v2입니다.")])
    no_setup_error(out)
    obs = s.selected[-1].observations
    assert obs["r3"]["status"] == "ok" and "no_progress_repeat" not in out
    assert s.tasks()[-1]["run_budget"]["last_progress"]["call"] == "r3"


# ==== 3. final verify -> fix -> return, end to end ==========================================================
@pytest.mark.asyncio
async def test_final_fix_A_sufficient_budget(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 6, 30)
    out = await s.turn(GOLF, golf_design(), steps=[final(A2_WRONG), final(A2)])
    no_setup_error(out)
    sel = s.selected[-1]
    assert sel.trace.counters.get("answer_fix_requests") == 1      # error found, one fix return
    assert budget_of(s).sends == len(s.mock.requests) == 3          # design + wrong + fix: same budget
    assert sel.calculation_status()["overall"] != "mismatch"       # re-verified
    assert sum(minutes(out).values()) == 45 and s.tasks()[-1]["status"] == "DONE"


@pytest.mark.asyncio
async def test_final_fix_B_budget_ends_before_fix(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 2, 30)
    out = await s.turn(GOLF, golf_design(), steps=[final(A2_WRONG), final(A2)])
    no_setup_error(out)
    assert len(s.mock.requests) == 2 and len(s.mock.steps) == 1     # no extra send; report needed none
    assert "budget_sends" in out and "중단 이유" in out
    task = s.tasks()[-1]
    assert task["status"] != "DONE" and task["unfinished"]          # unmet requirement preserved
    assert task["run_budget"]["sends"] == 2


@pytest.mark.asyncio
async def test_final_fix_C_deadline_passes_during_fix(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 10, 1.0)
    sleepy(s)
    t0 = time.monotonic()
    out = await s.turn(GOLF, golf_design(), steps=[final(A2_WRONG), ("sleep", 5, final(A2)), final(A2)])
    no_setup_error(out)
    assert time.monotonic() - t0 < 3                                # remaining-time limit applied
    b = budget_of(s)
    assert b.stop_reason == "budget_deadline" and len(s.mock.requests) == 3   # no further fix / repair
    assert len(s.mock.steps) == 1
    snap = s.tasks()[-1]["run_budget"]
    assert snap["unknown_usage_calls"] == 3 and snap["tokens_known"] == 0     # unknown stays unknown
    assert "budget_deadline" in out


@pytest.mark.asyncio
async def test_final_fix_count_limit_kept(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 20, 30)
    out = await s.turn(GOLF, golf_design(), steps=[final(A2_WRONG)] * 8)
    no_setup_error(out)
    fixes = s.selected[-1].trace.counters.get("answer_fix_requests", 0)
    assert 1 <= fixes <= 2 and len(s.mock.requests) <= 1 + 1 + fixes
    assert s.tasks()[-1]["status"] != "DONE"


# ==== 4. a local shell process really ended ================================================================
def _alive(token):
    r = subprocess.run(["pgrep", "-f", token], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def _sizes(*paths):
    return [p.stat().st_size if p.exists() else 0 for p in paths]


@pytest.mark.asyncio
async def test_exec_process_and_child_end_at_deadline(tmp_path, monkeypatch):
    s = explicit_session(tmp_path, monkeypatch, 10, 1.5)
    token = "T55MARK" + uuid.uuid4().hex[:8]
    parent, child = tmp_path / "m_parent", tmp_path / "m_child"
    cmd = (": %s; (while :; do echo c >> %s; sleep 0.05; done) & "
           "while :; do echo p >> %s; sleep 0.05; done" % (token, child, parent))
    t0 = time.monotonic()
    out = await s.turn("m_parent 파일에 표식을 기록해 주세요", exe(), steps=[calls(("e1", "exec", {"command": cmd, **meta(str(parent))})),
                                               final("끝")])
    waited = time.monotonic() - t0
    no_setup_error(out)
    assert waited < 4                                               # the tool wait ended
    assert _sizes(parent)[0] > 0 and _sizes(child)[0] > 0           # both really ran
    await asyncio.sleep(0.3)
    left = _alive(token)
    before = _sizes(parent, child)
    await asyncio.sleep(0.4)
    after = _sizes(parent, child)
    assert not left, "processes still alive: %s" % left             # own process + child ended
    assert before == after                                          # no marker after termination
    assert len(s.executor_requests()) == 1 and "budget_deadline" in out
    assert "종료 확인 불가" not in out
    info = getattr(s.loop.tools.get("exec"), "last_termination", None)
    assert info is None or info["confirmed"] is True                 # (None on e08d7ac: not recorded)
    assert not getattr(s.selected[-1], "unconfirmed_terminations", [])


def test_unconfirmed_termination_is_reported_as_unknown(monkeypatch):
    from nanobot.agent.tools import shell
    r = shell.termination_note({"pid": 4242, "pgid": 4242, "confirmed": False, "command": "x"})
    assert "종료 확인 불가·결과 불명" in r and "4242" in r
    assert shell.termination_note({"pid": 1, "pgid": 1, "confirmed": True, "command": "x"}) == ""


@pytest.mark.asyncio
async def test_kill_process_confirms_only_its_own_group(tmp_path):
    from nanobot.agent.tools.shell import ExecTool
    other = subprocess.Popen(["sleep", "5"], start_new_session=True)    # unrelated: must survive
    proc = await asyncio.create_subprocess_shell("sleep 5 & sleep 5", start_new_session=True)
    try:
        info = await ExecTool._kill_process(proc)
        assert info["confirmed"] is True and info["pgid"] == proc.pid
        assert other.poll() is None
    finally:
        other.kill(); other.wait()

"""t54 local verification: whole-request budget, repeat termination, deadline, persistence/resume, stop
report and the omitted-condition selection. MOCK model + real AgentLoop / selector / runner / provider
gate / task record. Nothing here measures a real model's completion rate or token use."""
import asyncio
import time

import pytest

from nanobot.agent import task_budget
from nanobot.agent.context_trace import ContextTrace, active_context_trace
from nanobot.agent.task_budget import TaskBudget
from nanobot.providers.base import LLMProvider, LLMResponse

from .t51_harness import Session, calls, design, final
from .test_context_selection import env as env  # noqa: F401
from .test_context_t51_local import meta, write

SETUP = "unexpected executor call"


def cfg(s, **kw):
    for k, v in kw.items():
        setattr(s.loop.context_selection_config, k, v)


def no_setup_error(out):
    if out and SETUP in out:
        pytest.fail("TEST SETUP ERROR (mock response not prepared): " + out[-200:])


def rd(cid, path):
    return calls((cid, "read_file", {"path": str(path)}))


def exe(**kw):
    return design(goal="작업", route="execute", request_type="execute",
                  capabilities_needed=["read_file", "write_file", "exec"], **kw)


def budget_of(s):
    return s.selected[-1].trace.budget


# ---- 1. same action / same error repeated -> finite stop -------------------------------------------------
@pytest.mark.asyncio
async def test_same_failing_action_repeated_stops_in_finite_steps(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    missing = tmp_path / "missing.md"
    out = await s.turn("missing.md를 읽어 요약해 주세요", exe(),
                       steps=[rd("r%d" % i, missing) for i in range(30)])
    no_setup_error(out)
    used = len(s.executor_requests())
    assert used < 12, used                                    # not 30: stopped by the repeat rule
    assert "no_progress_repeat" in out and "중단 이유" in out
    assert len(s.mock.steps) > 0                              # the rest was never requested


# ---- 2. A -> B -> A with nothing changed is a repeat ------------------------------------------------------
@pytest.mark.asyncio
async def test_alternating_a_b_a_without_change_is_a_repeat(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    a, b = tmp_path / "a_missing.md", tmp_path / "b_missing.md"
    steps = [rd("x%d" % i, a if i % 2 == 0 else b) for i in range(30)]
    out = await s.turn("두 파일을 읽어 주세요", exe(), steps=steps)
    no_setup_error(out)
    assert len(s.executor_requests()) < 16
    assert "no_progress_repeat" in out
    plan = s.selected[-1].exec_plan
    assert any(e["target"] == str(a) and e["count"] >= 2 for e in plan["no_progress"])


# ---- 3. design, repair, executor, final fix share one budget --------------------------------------------
@pytest.mark.asyncio
async def test_design_repair_and_executor_share_one_budget(tmp_path, monkeypatch):
    from nanobot.providers.base import ToolCallRequest
    s = Session(tmp_path, monkeypatch)
    cfg(s, task_generation_attempts=4)
    bad = LLMResponse(content=None, usage={}, tool_calls=[ToolCallRequest(
        id="design", name="submit_context_design", arguments={"goal": 3})])
    s.mock.designs.append(bad)                                  # invalid -> one repair call
    out = await s.turn("a.md를 만들어 주세요", exe(), steps=[
        calls(write("w%d" % i, tmp_path / ("f%d.md" % i), "x\n")) for i in range(10)])
    no_setup_error(out)
    assert len(s.mock.requests) == 4                            # 2 design sends + 2 executor sends, total cap
    assert budget_of(s).sends == 4 and budget_of(s).stop_reason == "budget_sends"
    assert "budget_sends" in out and "중단 이유" in out


@pytest.mark.asyncio
async def test_unsupported_claim_return_draws_from_the_same_budget(tmp_path, monkeypatch):
    """A final answer claiming an unrecorded change is returned for correction by the existing guard; every
    such return is a send of the same request budget (here: design + 2 sends, then refused)."""
    s = Session(tmp_path, monkeypatch)
    cfg(s, task_generation_attempts=3)
    out = await s.turn("plan.md를 저장해 주세요", exe(), steps=[final("plan.md에 저장했습니다.")] * 6)
    no_setup_error(out)
    assert len(s.mock.requests) <= 3, len(s.mock.requests)
    assert budget_of(s).sends == len(s.mock.requests)


# ---- 4. provider internal retries are counted per actual send -------------------------------------------
class Flaky(LLMProvider):
    def __init__(self, delay=0.0):
        super().__init__()
        self.sent, self.delay, self.cleaned = 0, delay, 0

    def get_default_model(self):
        return "MOCK"

    async def chat(self, messages, **kw):
        self.sent += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return LLMResponse(content="Error: 503 service unavailable", finish_reason="error",
                               error_status_code=503, error_should_retry=True)
        finally:
            self.cleaned += 1

    async def chat_stream(self, messages, **kw):
        return await self.chat(messages, **kw)


@pytest.mark.asyncio
async def test_provider_internal_retries_are_limited_per_actual_send(monkeypatch):
    async def no_wait(*a, **k):
        return None
    monkeypatch.setattr(LLMProvider, "_sleep_with_heartbeat", no_wait)
    p = Flaky()
    trace = ContextTrace(request_id="r")
    trace.budget = TaskBudget("r", max_sends=2)
    tok = active_context_trace.set(trace)
    try:
        r = await p.chat_with_retry(messages=[{"role": "user", "content": "x"}])
    finally:
        active_context_trace.reset(tok)
    assert p.sent == 2 and trace.budget.sends == 2
    assert r.error_kind == "budget_exhausted" and r.error_should_retry is False


# ---- 5. long model wait / stream -> deadline + finite cleanup ---------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_long_model_wait_is_cut_at_the_request_deadline(stream):
    p = Flaky(delay=30)
    trace = ContextTrace(request_id="r")
    trace.budget = TaskBudget("r", deadline_s=0.05)
    tok = active_context_trace.set(trace)
    t0 = time.monotonic()
    try:
        fn = p._safe_chat_stream if stream else p._safe_chat
        r = await fn(messages=[{"role": "user", "content": "x"}])
        again = await p._safe_chat(messages=[{"role": "user", "content": "x"}])
    finally:
        active_context_trace.reset(tok)
    assert time.monotonic() - t0 < 2
    assert r.error_kind == "budget_exhausted" and p.cleaned == 1      # the waiting call was cancelled
    assert again.error_kind == "budget_exhausted" and p.sent == 1      # no new send after the deadline


@pytest.mark.asyncio
async def test_retry_wait_never_outlives_the_deadline(monkeypatch):
    p = Flaky()
    trace = ContextTrace(request_id="r")
    trace.budget = TaskBudget("r", deadline_s=0.2)
    tok = active_context_trace.set(trace)
    t0 = time.monotonic()
    try:
        r = await p.chat_with_retry(messages=[{"role": "user", "content": "x"}])   # retry delays 1,2,4 s
    finally:
        active_context_trace.reset(tok)
    assert time.monotonic() - t0 < 1.0 and p.sent == 1
    assert r.error_kind == "budget_exhausted"


# ---- 6/8. tool wait cut by the deadline; the change stays unknown and is not repeated ------------------------
@pytest.mark.asyncio
async def test_tool_wait_deadline_keeps_change_unknown_and_no_duplicate_write(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    cfg(s, task_deadline_seconds=1.5)
    f = tmp_path / "log.txt"
    cmd = "sleep 4; echo x >> " + str(f)
    t0 = time.monotonic()
    out = await s.turn("log.txt에 한 줄 추가해 주세요", exe(), steps=[
        calls(("e1", "exec", {"command": cmd, **meta(str(f))})),
        calls(("e2", "exec", {"command": cmd, **meta(str(f))}))])
    no_setup_error(out)
    assert time.monotonic() - t0 < 6
    assert "budget_deadline" in out
    assert len(s.executor_requests()) == 1                      # no new work after the deadline
    await asyncio.sleep(0)
    assert not f.exists() or f.read_text().count("x") <= 1      # never written twice


# ---- 6. normal multi-step work is not judged as no progress ------------------------------------------------
@pytest.mark.asyncio
async def test_normal_multi_step_work_is_not_no_progress(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    names = ["p%d.md" % i for i in range(6)]
    out = await s.turn("파일 6개를 만들어 주세요", exe(), steps=[
        *(calls(write("w%d" % i, tmp_path / n, "내용 %d\n" % i)) for i, n in enumerate(names)),
        final("6개 파일을 만들었습니다.")])
    no_setup_error(out)
    assert all((tmp_path / n).exists() for n in names)
    assert "no_progress_repeat" not in out and "budget_" not in out
    assert budget_of(s).stop_reason is None


# ---- 7. one requirement's failures are not reset by another's success ----------------------------------------
@pytest.mark.asyncio
async def test_failing_requirement_not_reset_by_other_success(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    bad = tmp_path / "nope" / "missing.md"
    steps = []
    for i in range(8):                       # the other requirement keeps succeeding with new material
        (tmp_path / ("ok%d.md" % i)).write_text("자료 %d\n" % i)
        steps.append(rd("r%d" % i, bad))
        steps.append(rd("k%d" % i, tmp_path / ("ok%d.md" % i)))
    out = await s.turn("작업", exe(), steps=steps)
    no_setup_error(out)
    plan = s.selected[-1].exec_plan
    entry = [e for e in plan["no_progress"] if e["target"] == str(bad)]
    assert entry and entry[0]["count"] >= 2                     # history kept across the other successes
    assert "no_progress_repeat" in out
    assert not any(e["target"].endswith("ok0.md") for e in plan["no_progress"])


# ---- 9. resume after the cap: no renewal without the user, segments keep history --------------------------
def test_same_request_restore_is_not_a_new_budget():
    b1 = TaskBudget("req1", max_sends=3)
    for _ in range(3):
        assert b1.acquire() is None
    assert b1.acquire() == "budget_sends"
    snap = b1.snapshot()
    b2 = TaskBudget("req1", max_sends=3, prior=snap)              # process resume / re-entry
    assert b2.acquire() == "budget_sends" and b2.sends == 3
    b3 = TaskBudget("cont", max_sends=3)
    b3.restore(snap, same_request=True)                         # internal continuation
    assert b3.acquire() is not None
    b4 = TaskBudget("req2", max_sends=3, prior=snap)              # explicit new user message
    assert b4.acquire() is None and b4.segments[-1]["sends"] == 3 and b4.total_sends() == 4


def test_unknown_token_usage_is_not_zero():
    b = TaskBudget("r", max_tokens=100)
    b.record_usage({"total_tokens": 60})
    assert b.token_cap_enforceable()
    b.record_usage({})
    assert not b.token_cap_enforceable() and b.unknown_usage == 1
    assert b.snapshot()["token_cap"] == "unenforceable_unknown_usage"
    b2 = TaskBudget("r", max_tokens=100, max_sends=1)
    b2.record_usage({"total_tokens": 100})
    assert b2.acquire() == "budget_tokens"


def test_clock_is_controllable_and_non_generation_counted_apart(monkeypatch):
    t = [100.0]
    monkeypatch.setattr(task_budget, "now", lambda: t[0])
    b = TaskBudget("r", deadline_s=10)
    b.note_non_generation()
    t[0] += 4
    assert b.non_generation == 1 and b.sends == 0 and b.remaining_s() == 6 and b.cap(30) == 6
    t[0] += 7
    assert b.acquire() == "budget_deadline"


@pytest.mark.asyncio
async def test_resume_after_cap_reuses_record_and_needs_user(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    cfg(s, task_generation_attempts=2)
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    out = await s.turn("a.md와 b.md를 만들어 주세요", exe(), steps=[
        calls(write("wa", a, "A\n")), calls(write("wb", b, "B\n")), final("완료")])
    no_setup_error(out)
    assert "budget_sends" in out and "이어가기" in out and a.exists() and not b.exists()
    task = s.tasks()[-1]
    assert task["run_budget"]["stop_reason"] == "budget_sends" and task["run_budget"]["sends"] == 2
    assert task.get("execution_plan")                            # requirements / facts persisted
    s.mock.steps.clear()
    # explicit user "continue": a new segment, earlier consumption kept as history
    out2 = await s.turn("계속", exe(task_relation="continue", task_id=task["task_id"]), steps=[calls(write("wb2", b, "B\n")), final("b.md도 만들었습니다.")])
    no_setup_error(out2)
    rb = s.tasks()[-1]["run_budget"]
    assert rb["segments"] and rb["segments"][-1]["stop_reason"] == "budget_sends"
    assert a.read_text() == "A\n"                               # the earlier success not redone


# ---- 10. stop report: records only, no extra model call ---------------------------------------------------
@pytest.mark.asyncio
async def test_stop_report_needs_no_model_call_and_matches_state(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    cfg(s, task_generation_attempts=3)
    out = await s.turn("x.md를 만들어 주세요", exe(), steps=[
        calls(write("w1", tmp_path / "x.md", "x\n")), calls(write("w2", tmp_path / "y.md", "y\n")), final("끝")])
    no_setup_error(out)
    assert len(s.mock.requests) == 3                            # nothing sent for the report
    snap = s.tasks()[-1]["run_budget"]
    assert ("생성 호출 " + str(snap["sends"]) + "회") in out
    assert out.count("중단 이유") == 1


# ---- 11. omitted condition fetched by lookup: valid accepted, invalid rejected ------------------------------
def _payload(status="user_original", task="T1", sid="m1", omitted=("m1",)):
    return {"request": "45분으로", "sources": [],
            "prior_context": {"task_candidates": ["T1", "T2"], "current_task_id": "T1",
                              "previous_answer": {"ref_id": "ans1"}, "kept_conditions": [],
                              "omitted_condition_sources": list(omitted)},
            "task_state": {"task_id": task, "valid_constraint_candidates": [
                {"source_id": sid, "text": "드라이버는 제외", "status": status}]}}


def _ref(task_id="T1", sid="m1", text="드라이버는 제외"):
    return design(goal="g", request_type="modify", answer="x", reference={
        "relation": "modify_answer", "task_id": task_id, "response_id": "ans1",
        "kept_conditions": [{"text": text, "source_id": sid}], "changed_conditions": []})


@pytest.mark.parametrize("payload,ref,ok", [
    (_payload(), _ref(), True),
    (_payload(omitted=()), _ref(), False),                         # not an omitted source
    (_payload(), _ref(sid="m9"), False),                           # nonexistent source
    (_payload(), _ref(task_id="T2"), False),                       # another task
    (_payload(status="cancelled"), _ref(), False),
    (_payload(status="superseded"), _ref(), False),
    (_payload(), _ref(text="아이언은 제외"), False),                 # text not in the valid condition
])
def test_omitted_condition_selection(payload, ref, ok):
    from nanobot.agent.context_selection import reference_check
    errors, _ = reference_check(ref, payload)
    assert ("reference_kept_condition_unoffered" not in errors) is ok, errors

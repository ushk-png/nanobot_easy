"""A background job that ends while no turn is waiting on it must wake the agent that started it.

The job log and ``exec_job status`` are pull-only: once the model ends its turn, nothing reads them again, so a
finished job was silently ignored until the user asked about it.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.exec_job import ExecJobManager, exec_job_notice
from nanobot.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


class Recorder:
    def __init__(self) -> None:
        self.jobs: list = []
        self.event = asyncio.Event()

    async def __call__(self, job) -> None:
        self.jobs.append(job)
        self.event.set()


async def start(manager, tmp_path, command, *, context=True, **kwargs):
    tool = ExecTool(working_dir=str(tmp_path), job_manager=manager)
    token = None
    if context:
        token = bind_request_context(RequestContext(
            channel="telegram", chat_id="42", message_id="m1", session_key="telegram:42"))
    try:
        return await tool.execute(command=command, background=True, **kwargs)
    finally:
        if token is not None:
            reset_request_context(token)


async def test_a_finished_job_is_announced_once_with_its_origin(tmp_path):
    manager, seen = ExecJobManager(), Recorder()
    manager.set_announcer(seen)
    await start(manager, tmp_path, "echo done-marker; exit 3")

    await asyncio.wait_for(seen.event.wait(), 5)
    await asyncio.sleep(0.1)

    assert len(seen.jobs) == 1
    job = seen.jobs[0]
    assert (job.state, job.returncode) == ("exited", 3)
    assert job.origin == {"channel": "telegram", "chat_id": "42", "message_id": "m1",
                          "session_key": "telegram:42"}


async def test_a_job_the_model_killed_is_not_announced(tmp_path):
    manager, seen = ExecJobManager(), Recorder()
    manager.set_announcer(seen)
    started = await start(manager, tmp_path, "sleep 30")
    job_id = started.split("job_id: ")[1].split()[0]

    await manager.kill(job_id, "telegram:42")
    await asyncio.sleep(0.2)

    assert seen.jobs == []


async def test_a_job_that_hit_its_timeout_is_announced(tmp_path):
    manager, seen = ExecJobManager(), Recorder()
    manager.set_announcer(seen)
    tool = ExecTool(working_dir=str(tmp_path), job_manager=manager, background_timeout=1)
    token = bind_request_context(RequestContext(channel="telegram", chat_id="42", session_key="telegram:42"))
    try:
        await tool.execute(command="sleep 30", background=True)
    finally:
        reset_request_context(token)

    await asyncio.wait_for(seen.event.wait(), 8)

    assert seen.jobs[0].state == "timed_out"


async def test_a_job_without_a_requesting_chat_is_not_announced(tmp_path):
    manager, seen = ExecJobManager(), Recorder()
    manager.set_announcer(seen)
    await start(manager, tmp_path, "true", context=False)

    await asyncio.sleep(0.4)

    assert seen.jobs == []


async def test_a_failing_announcer_does_not_break_the_job_record(tmp_path):
    manager = ExecJobManager()

    async def boom(job):
        raise RuntimeError("bus closed")

    manager.set_announcer(boom)
    started = await start(manager, tmp_path, "exit 0")
    job_id = started.split("job_id: ")[1].split()[0]
    await asyncio.sleep(0.4)

    assert manager.get(job_id, "telegram:42").state == "exited"


def test_the_notice_carries_the_outcome_and_log_tail_for_the_origin_chat(tmp_path):
    manager = ExecJobManager()
    log = tmp_path / "j.log"
    log.write_text("one\ntwo\nall tests passed\n")
    from nanobot.agent.tools.exec_job import ExecJob

    job = ExecJob(job_id="abc123", command="codewhale exec --auto", cwd=str(tmp_path), pid=1,
                  log_path=log, meta_path=tmp_path / "j.json", started_at=1.0, timeout=None,
                  owner_session_key="telegram:42", returncode=0, finished_at=61.0,
                  origin={"channel": "telegram", "chat_id": "42", "message_id": "m1",
                          "session_key": "telegram:42"})

    msg = exec_job_notice(job)

    assert (msg.channel, msg.sender_id, msg.chat_id) == ("system", "subagent", "telegram:42")
    assert msg.session_key_override == "telegram:42"
    assert msg.metadata["injected_event"] == "subagent_result"
    assert msg.metadata["subagent_task_id"] == "exec-job-abc123"
    assert "abc123" in msg.content and "exit_code: 0" in msg.content
    assert "all tests passed" in msg.content
    assert manager is not None


def test_the_notice_warns_when_the_log_shows_an_unanswerable_permission_prompt(tmp_path):
    from nanobot.agent.tools.exec_job import ExecJob

    log = tmp_path / "j.log"
    log.write_text("tool Run failed: Tool 'Run' denied by user - the call was not approved.\n")
    job = ExecJob(job_id="abc", command="codewhale exec x", cwd=str(tmp_path), pid=1, log_path=log,
                  meta_path=tmp_path / "j.json", started_at=1.0, timeout=None, returncode=0, finished_at=2.0,
                  origin={"channel": "telegram", "chat_id": "42", "session_key": "telegram:42"})

    content = exec_job_notice(job).content

    assert "probably did not do the work" in content
    assert "Do not start the same command again" not in content

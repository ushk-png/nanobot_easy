"""Codewhale stays detached past the foreground exec cap."""

from __future__ import annotations

import asyncio
import re
import stat
import sys

import pytest

from nanobot.agent.tools.codewhale import (
    CodewhaleTool,
    build_codewhale_command,
    is_codewhale_agent_command,
)
from nanobot.agent.tools.exec_job import ExecJobManager, ExecJobTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import ToolsConfig

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


def _job_id(text: str) -> str:
    match = re.search(r"job_id: ([0-9a-f]+)", text)
    assert match, text
    return match.group(1)


def _binary(tmp_path):
    path = tmp_path / "codewhale"
    path.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_agent_command_detection_ignores_short_subcommands():
    assert is_codewhale_agent_command("/opt/bin/codewhale -C /repo exec 'fix the test'")
    assert is_codewhale_agent_command("codewhale exec --resume abc123 'continue'")
    assert not is_codewhale_agent_command("codewhale doctor")
    assert not is_codewhale_agent_command("codewhale --version")
    assert not is_codewhale_agent_command("echo codewhale exec 'not the program'")


def test_command_builder_quotes_and_omits_secrets():
    command = build_codewhale_command(
        "/opt/bin/codewhale",
        "/repo/my project",
        "keep the token out",
        "abc123",
    )
    assert command.startswith("/opt/bin/codewhale -C '/repo/my project' exec --auto --resume abc123 ")
    assert "keep the token out" in command
    assert "--api-key" not in command


def test_config_accepts_codewhale_settings():
    config = ToolsConfig(codewhale={"enable": True, "binary": "/opt/bin/codewhale", "timeout": 0})
    assert config.codewhale.enable is True
    assert config.codewhale.timeout == 0
    assert CodewhaleTool.enabled(type("Ctx", (), {"config": config})()) is True
    assert CodewhaleTool.enabled(type("Ctx", (), {"config": ToolsConfig()})()) is False


async def test_exec_detaches_codewhale_instead_of_killing_at_the_cap(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    tool = ExecTool(working_dir=str(tmp_path), timeout=1, job_manager=manager)
    jobs = ExecJobTool(manager=manager, workspace=str(tmp_path))

    started = await tool.execute(
        command=f"{binary} exec 'write the feature'",
        timeout=10,
    )
    job_id = _job_id(started)
    try:
        assert "Background job started" in started
        assert "600s foreground exec cap" in started
        assert manager.get(job_id).timeout is None
        assert "state: running" in await jobs.execute(action="status", job_id=job_id)
    finally:
        await manager.kill(job_id)


async def test_short_codewhale_command_still_uses_the_foreground_cap(tmp_path):
    binary = _binary(tmp_path)
    tool = ExecTool(working_dir=str(tmp_path), timeout=1)
    result = await tool.execute(command=f"{binary} doctor", timeout=1)
    assert "Background job started" not in result
    assert "timed out" in result


async def test_codewhale_tool_starts_one_detached_run(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    tool = CodewhaleTool(
        workspace=tmp_path,
        binary=str(binary),
        timeout=0,
        job_manager=manager,
    )

    started = await tool.execute(action="run", prompt="add a regression test")
    job_id = _job_id(started)
    try:
        command = manager.get(job_id).command
        assert "--api-key" not in command
        assert f"-C {tmp_path}" in command or f"-C '{tmp_path}'" in command
        assert "exec" in command
        assert manager.get(job_id).timeout is None
        status = await tool.execute(action="status", job_id=job_id)
        assert "state: running" in status
        again = await tool.execute(action="run", prompt="do it again")
        assert again.is_error
        assert "still running" in again
        rejected = await tool.execute(action="run", prompt="resume", session_id="bad;id")
        assert rejected.is_error
        assert "session_id" in rejected
    finally:
        await manager.kill(job_id)


def test_a_run_has_automatic_approvals_because_nobody_can_answer_a_prompt():
    assert " exec --auto " in build_codewhale_command("/b/cw", "/repo", "fix it")
    assert "--auto" not in build_codewhale_command("/b/cw", "/repo", "fix it", auto=False)


def test_sandbox_policy_is_passed_and_validated():
    assert " --sandbox workspace-write " in build_codewhale_command(
        "/b/cw", "/repo", "fix it", sandbox="workspace-write")
    with pytest.raises(ValueError):
        build_codewhale_command("/b/cw", "/repo", "fix it", sandbox="x; rm -rf /")


def test_a_prompt_that_starts_with_a_dash_is_not_read_as_a_flag():
    assert " -- '-v makes it loud'" in build_codewhale_command("/b/cw", "/repo", "-v makes it loud")


def test_a_workspace_restricted_agent_keeps_the_codewhale_shell_sandboxed(tmp_path):
    assert CodewhaleTool(workspace=tmp_path, restrict_to_workspace=True).sandbox == "workspace-write"
    assert CodewhaleTool(workspace=tmp_path).sandbox == ""
    assert CodewhaleTool(workspace=tmp_path, restrict_to_workspace=True, sandbox="read-only").sandbox == "read-only"


async def test_the_tool_starts_codewhale_with_automatic_approvals(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    tool = CodewhaleTool(workspace=tmp_path, binary=str(binary), job_manager=manager)
    job_id = _job_id(await tool.execute(action="run", prompt="add a test"))
    try:
        assert " exec --auto " in manager.get(job_id).command
    finally:
        await manager.kill(job_id)


async def test_a_run_that_stopped_at_a_permission_prompt_is_not_reported_as_done(tmp_path):
    script = tmp_path / "codewhale"
    script.write_text(
        "#!/bin/sh\necho \"tool Run failed: Tool 'Run' denied by user - the call was not approved.\"\nexit 0\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    manager = ExecJobManager()
    tool = CodewhaleTool(workspace=tmp_path, binary=str(script), job_manager=manager)
    job_id = _job_id(await tool.execute(action="run", prompt="add a test"))
    for _ in range(100):
        status = await tool.execute(action="status", job_id=job_id)
        if "state: exited" in status:
            break
        await asyncio.sleep(0.05)

    assert "exit_code: 0" in status
    assert "denied permission" in status and "not a result" in status


# ---- item 4: the one-run guard sees session-owned jobs ------------------------------------------

from nanobot.agent.tools.context import (  # noqa: E402
    RequestContext,
    bind_request_context,
    reset_request_context,
)


async def _start_owned(manager, tmp_path, owner, workspace, *, finished=False):
    command = "true" if finished else "sleep 30"
    job = await manager.start(
        command=command,
        display_command=f"/opt/bin/codewhale -C {workspace} exec --auto 'task'",
        cwd=str(tmp_path), env={"PATH": "/bin:/usr/bin"}, timeout=60, shell_program=None,
        login=False, log_dir=tmp_path / "logs", owner_session_key=owner,
    )
    if finished:
        for _ in range(100):
            if job.returncode is not None:
                break
            await asyncio.sleep(0.02)
    return job


def _bind(session_key):
    return bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key=session_key))


async def test_guard_sees_a_running_job_owned_by_a_session(tmp_path):
    manager = ExecJobManager()
    job = await _start_owned(manager, tmp_path, "telegram:123", tmp_path / "w")
    try:
        tool = CodewhaleTool(workspace=tmp_path, job_manager=manager)
        assert tool._running_job_id() == job.job_id
    finally:
        await manager.kill(job.job_id, "telegram:123")


async def test_same_session_second_run_is_refused_even_in_another_workspace(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    job = await _start_owned(manager, tmp_path, "telegram:a", tmp_path / "one")
    token = _bind("telegram:a")
    try:
        tool = CodewhaleTool(workspace=tmp_path, binary=str(binary), job_manager=manager)
        other = tmp_path / "two"
        other.mkdir()
        again = await tool.execute(action="run", prompt="second", workspace=str(other))
        assert again.is_error
        assert job.job_id in again and "still running" in again
    finally:
        reset_request_context(token)
        await manager.kill(job.job_id, "telegram:a")


async def test_other_session_same_workspace_is_refused_without_its_job_id(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    job = await _start_owned(manager, tmp_path, "telegram:a", tmp_path)
    token = _bind("telegram:b")
    try:
        tool = CodewhaleTool(workspace=tmp_path, binary=str(binary), job_manager=manager)
        again = await tool.execute(action="run", prompt="second")
        assert again.is_error
        assert job.job_id not in again
        assert "다른 대화에서 같은 작업공간을 수정하는 코드웨일 작업이 실행 중입니다" in again
    finally:
        reset_request_context(token)
        await manager.kill(job.job_id, "telegram:a")


async def test_other_session_other_workspace_is_allowed(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    job = await _start_owned(manager, tmp_path, "telegram:a", tmp_path / "elsewhere")
    token = _bind("telegram:b")
    started_id = None
    try:
        tool = CodewhaleTool(workspace=tmp_path, binary=str(binary), job_manager=manager)
        started = await tool.execute(action="run", prompt="mine")
        assert not getattr(started, "is_error", False), started
        started_id = _job_id(started)
    finally:
        reset_request_context(token)
        await manager.kill(job.job_id, "telegram:a")
        if started_id:
            await manager.kill(started_id, "telegram:b")


async def test_finished_codewhale_jobs_do_not_block(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    await _start_owned(manager, tmp_path, None, tmp_path, finished=True)
    tool = CodewhaleTool(workspace=tmp_path, binary=str(binary), job_manager=manager)
    assert tool._running_job_id() is None
    started = await tool.execute(action="run", prompt="go")
    job_id = _job_id(started)
    await manager.kill(job_id)


async def test_job_tool_owner_isolation_is_unchanged(tmp_path):
    manager = ExecJobManager()
    job = await _start_owned(manager, tmp_path, "telegram:a", tmp_path)
    try:
        assert manager.get(job.job_id, "telegram:b") is None
        assert manager.list("telegram:b") == []
        assert [j.job_id for j in manager.list("telegram:a")] == [job.job_id]
    finally:
        await manager.kill(job.job_id, "telegram:a")


# ---- item 5: codewhale runs inherit the operator's exec protections -----------------------------

def _ctx(tmp_path, binary, exec_cfg=None, restrict=False):
    config = ToolsConfig(
        codewhale={"enable": True, "binary": str(binary)},
        exec=exec_cfg or {},
        restrict_to_workspace=restrict,
    )
    return type("Ctx", (), {"config": config, "workspace": str(tmp_path)})()


async def _cleanup(manager):
    for job in manager.list_all():
        if job.returncode is None:
            await manager.kill(job.job_id, job.owner_session_key)


async def test_configured_deny_pattern_blocks_a_codewhale_run(tmp_path):
    binary = _binary(tmp_path)
    tool = CodewhaleTool.create(_ctx(tmp_path, binary, {"deny_patterns": ["secretword"]}))
    tool._manager = manager = ExecJobManager()
    try:
        result = await tool.execute(action="run", prompt="please handle secretword now")
        assert getattr(result, "is_error", False)
        assert "deny pattern" in str(result)
    finally:
        await _cleanup(manager)


async def test_empty_deny_patterns_still_run(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    tool = CodewhaleTool.create(_ctx(tmp_path, binary, {"deny_patterns": []}))
    tool._manager = manager
    job_id = _job_id(await tool.execute(action="run", prompt="plain task"))
    await manager.kill(job_id)


async def test_allow_patterns_do_not_block_codewhale(tmp_path):
    binary = _binary(tmp_path)
    manager = ExecJobManager()
    tool = CodewhaleTool.create(_ctx(tmp_path, binary, {"allow_patterns": [r"git status"]}))
    tool._manager = manager
    started = await tool.execute(action="run", prompt="plain task")
    assert not getattr(started, "is_error", False), started
    await manager.kill(_job_id(started))


async def test_exec_settings_reach_the_internal_exec_tool(tmp_path, monkeypatch):
    import nanobot.agent.tools.shell as shell

    seen = {}

    class Spy(shell.ExecTool):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

        def _prepare_command(self, *args, **kwargs):
            return "stopped before spawning"

    monkeypatch.setattr(shell, "ExecTool", Spy)
    binary = _binary(tmp_path)
    tool = CodewhaleTool.create(_ctx(tmp_path, binary, {
        "sandbox": "bwrap", "path_append": "/opt/extra/bin", "path_prepend": "/opt/first/bin",
        "allowed_env_keys": ["MY_KEY"], "deny_patterns": ["secretword"], "allow_patterns": ["git status"],
    }))
    tool._manager = ExecJobManager()
    assert await tool.execute(action="run", prompt="task") == "stopped before spawning"
    assert seen["sandbox"] == "bwrap"
    assert seen["path_append"] == "/opt/extra/bin" and seen["path_prepend"] == "/opt/first/bin"
    assert seen["allowed_env_keys"] == ["MY_KEY"]
    assert seen["deny_patterns"] == ["secretword"]
    assert not seen.get("allow_patterns")


async def test_restricted_agent_refuses_a_workspace_outside_its_own(tmp_path):
    binary = _binary(tmp_path)
    inside = tmp_path / "ws"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = ToolsConfig(codewhale={"enable": True, "binary": str(binary)}, restrict_to_workspace=True)
    tool = CodewhaleTool.create(type("Ctx", (), {"config": config, "workspace": str(inside)})())
    tool._manager = manager = ExecJobManager()
    try:
        result = await tool.execute(action="run", prompt="task", workspace=str(outside))
        assert getattr(result, "is_error", False)
        assert "outside the configured workspace" in str(result)
    finally:
        await _cleanup(manager)


@pytest.mark.parametrize("command", [
    "sleep 999999; codewhale exec x",
    "yes > /dev/null || codewhale exec x",
    "cat big.log | codewhale exec x",
    "echo hi && codewhale exec x",
])
def test_a_codewhale_segment_inside_a_longer_command_is_not_detached(command) -> None:
    """Review N12: such a command used to run in the background with no timeout at all."""
    from nanobot.agent.tools.codewhale import is_codewhale_agent_command

    assert is_codewhale_agent_command(command, standalone=True) is False


def test_a_plain_codewhale_run_is_still_detached() -> None:
    from nanobot.agent.tools.codewhale import is_codewhale_agent_command

    assert is_codewhale_agent_command("codewhale exec 'fix; the bug && more'", standalone=True) is True
    assert is_codewhale_agent_command("codewhale -C /work exec 'task'", standalone=True) is True


def test_unquoted_separators_are_found_even_without_spaces() -> None:
    from nanobot.agent.tools.codewhale import is_codewhale_agent_command

    for command in ("sleep 9;codewhale exec x", "sleep 9&codewhale exec x", "true\ncodewhale exec x"):
        assert is_codewhale_agent_command(command, standalone=True) is False

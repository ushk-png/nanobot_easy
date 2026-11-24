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

"""exec tool against real subprocesses (no mocks): the paths mock-based tests missed."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from nanobot.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")


async def _wait_for_pid(pid_file: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = pid_file.read_text().strip() if pid_file.exists() else ""
        if text:
            return int(text)
        await asyncio.sleep(0.02)
    raise AssertionError("background child never reported its pid")


async def _assert_process_dies(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    os.kill(pid, 9)  # do not leak the child if the assertion below fails
    raise AssertionError(f"process {pid} was left running")


async def test_reports_stdout_stderr_and_exit_code() -> None:
    result = await ExecTool(timeout=10).execute(command="echo out; echo err >&2; exit 3")

    assert "out" in result and "STDERR:" in result and "err" in result
    assert "Exit code: 3" in result


async def test_timeout_stops_a_command_that_keeps_running() -> None:
    started = time.monotonic()
    result = await asyncio.wait_for(
        ExecTool(timeout=1).execute(command="echo start; sleep 30"), timeout=10,
    )

    assert "timed out" in result.lower()
    assert time.monotonic() - started < 6


@pytest.mark.parametrize("close_pipes", [False, True])
async def test_cancelling_a_turn_kills_the_command_and_its_children(tmp_path, close_pipes) -> None:
    """/stop cancels the running tool; the shell and anything it started must not survive.

    With ``close_pipes`` the output streams reach EOF while the process keeps running, so
    the cancellation lands in the wait for the process to exit, not in the output drain.
    """
    pid_file = tmp_path / "child.pid"
    prefix = "exec >&- 2>&-; " if close_pipes else ""
    command = f"{prefix}sleep 60 & echo $! > {pid_file}; wait"
    task = asyncio.create_task(ExecTool(timeout=120).execute(command=command))

    child_pid = await _wait_for_pid(pid_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_process_dies(child_pid)

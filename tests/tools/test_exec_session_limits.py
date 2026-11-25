"""Exec sessions enforce their limits without being polled, and keep memory bounded (review C04)."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from nanobot.agent.tools import exec_session
from nanobot.agent.tools.exec_session import ExecSessionManager


async def _wait_exit(session, seconds: float) -> bool:
    try:
        await asyncio.wait_for(session.process.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell commands")
async def test_timeout_kills_the_process_even_when_nobody_polls() -> None:
    manager = ExecSessionManager(max_sessions=2, idle_timeout=1800)
    session_id, _ = await manager.start(
        command="sleep 30", cwd="/tmp", env=dict(os.environ), timeout=1, shell_program=None,
        login=False, yield_time_ms=0, max_output_chars=1000,
    )
    session = manager._sessions[session_id]
    try:
        assert await _wait_exit(session, 6), "the 1 s time limit was not enforced"
        assert session._timed_out is True
    finally:
        await session.kill()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell commands")
async def test_idle_session_is_stopped_without_another_tool_call() -> None:
    manager = ExecSessionManager(max_sessions=2, idle_timeout=1)
    session_id, _ = await manager.start(
        command="sleep 30", cwd="/tmp", env=dict(os.environ), timeout=None, shell_program=None,
        login=False, yield_time_ms=0, max_output_chars=1000,
    )
    session = manager._sessions[session_id]
    try:
        assert await _wait_exit(session, 8), "the idle limit was not enforced"
    finally:
        await session.kill()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX shell commands")
async def test_unpolled_output_is_bounded_and_the_next_poll_reports_the_loss(monkeypatch) -> None:
    monkeypatch.setattr(exec_session, "MAX_BUFFERED_CHARS", 20_000)
    manager = ExecSessionManager(max_sessions=2, idle_timeout=1800)
    command = f"{sys.executable} -c \"import sys; sys.stdout.write('x' * 400000 + 'END'); sys.stdout.flush()\""
    session_id, first = await manager.start(
        command=command, cwd="/tmp", env=dict(os.environ), timeout=30, shell_program=None,
        login=False, yield_time_ms=0, max_output_chars=50_000,
    )
    session = manager._sessions.get(session_id)
    if session is not None:
        await _wait_exit(session, 10)
        await asyncio.sleep(0.3)
        assert session._buffered_chars <= 20_000 + 4096  # one read chunk of slack
        poll = await session.poll(0, 50_000)
        assert "earlier characters were discarded" in poll.output
        assert poll.output.rstrip().endswith("END")  # the newest output survives

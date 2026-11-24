"""The loop must publish a background job's completion to the bus so the owning session gets a turn."""

from __future__ import annotations

import asyncio
import sys

import pytest

from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.exec_job import DEFAULT_EXEC_JOB_MANAGER
from nanobot.agent.tools.shell import ExecTool
from tests.agent.conftest import make_loop

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


async def test_a_job_finishing_after_the_turn_ended_reaches_the_bus(tmp_path):
    loop = make_loop(tmp_path)
    tool = ExecTool(working_dir=str(tmp_path))          # shares DEFAULT_EXEC_JOB_MANAGER, as in production
    token = bind_request_context(RequestContext(
        channel="telegram", chat_id="42", message_id="m1", session_key="telegram:42"))
    try:
        await tool.execute(command="echo built-ok", background=True)
    finally:
        reset_request_context(token)

    msg = await asyncio.wait_for(loop.bus.consume_inbound(), 5)

    assert (msg.channel, msg.chat_id, msg.session_key_override) == ("system", "telegram:42", "telegram:42")
    assert "built-ok" in msg.content
    assert DEFAULT_EXEC_JOB_MANAGER is not None


async def test_the_notice_starts_a_turn_whose_reply_goes_to_the_chat_that_started_the_job(tmp_path):
    from unittest.mock import AsyncMock

    from nanobot.providers.base import LLMResponse

    loop = make_loop(tmp_path)
    reply = LLMResponse(content="코드웨일 작업이 끝났고 테스트가 통과했습니다.", finish_reason="stop")
    loop.provider.chat_with_retry = AsyncMock(return_value=reply)
    loop.provider.chat_stream_with_retry = AsyncMock(return_value=reply)
    tool = ExecTool(working_dir=str(tmp_path))
    token = bind_request_context(RequestContext(
        channel="telegram", chat_id="42", message_id="m1", session_key="telegram:42"))
    try:
        await tool.execute(command="echo built-ok", background=True)
    finally:
        reset_request_context(token)

    notice = await asyncio.wait_for(loop.bus.consume_inbound(), 5)
    await asyncio.wait_for(loop._dispatch(notice), 20)
    out = await asyncio.wait_for(loop.bus.consume_outbound(), 5)

    assert (out.channel, out.chat_id) == ("telegram", "42")
    assert "테스트가 통과" in out.content
    request = loop.provider.chat_with_retry.await_args or loop.provider.chat_stream_with_retry.await_args
    assert "built-ok" in str(request.kwargs["messages"])

"""Test cmd_stop drains pending queue to prevent mid-turn injection deadlock."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.command.builtin import cmd_stop
from nanobot.command.router import CommandContext


@pytest.mark.asyncio
async def test_cmd_stop_drains_pending_queue():
    """cmd_stop should drain pending queue in addition to cancelling active tasks."""
    mock_loop = MagicMock()
    mock_loop._cancel_active_tasks = AsyncMock(return_value=1)
    mock_loop._pending_queues = {}

    pending = asyncio.Queue()
    await pending.put("msg1")
    await pending.put("msg2")
    mock_loop._pending_queues["test-session"] = pending

    ctx = CommandContext(
        msg=MagicMock(channel="websocket", chat_id="test-chat", metadata={}),
        session=None,
        key="test-session",
        raw="/stop",
        loop=mock_loop,
    )

    result = await cmd_stop(ctx)

    assert isinstance(result, OutboundMessage)
    assert "Stopped 3 task(s)" in result.content  # 1 cancelled + 2 drained
    assert "test-session" not in mock_loop._pending_queues


@pytest.mark.asyncio
async def test_cmd_stop_with_empty_pending_queue():
    """cmd_stop should work correctly when pending queue is empty."""
    mock_loop = MagicMock()
    mock_loop._cancel_active_tasks = AsyncMock(return_value=2)
    mock_loop._pending_queues = {}

    pending = asyncio.Queue()
    mock_loop._pending_queues["test-session"] = pending

    ctx = CommandContext(
        msg=MagicMock(channel="websocket", chat_id="test-chat", metadata={}),
        session=None,
        key="test-session",
        raw="/stop",
        loop=mock_loop,
    )

    result = await cmd_stop(ctx)

    assert "Stopped 2 task(s)" in result.content
    assert "test-session" not in mock_loop._pending_queues


@pytest.mark.asyncio
async def test_cmd_stop_no_pending_queue():
    """cmd_stop should work when no pending queue exists."""
    mock_loop = MagicMock()
    mock_loop._cancel_active_tasks = AsyncMock(return_value=0)
    mock_loop._pending_queues = {}

    ctx = CommandContext(
        msg=MagicMock(channel="websocket", chat_id="test-chat", metadata={}),
        session=None,
        key="test-session",
        raw="/stop",
        loop=mock_loop,
    )

    result = await cmd_stop(ctx)

    assert "No active task to stop" in result.content


@pytest.mark.asyncio
async def test_stop_discards_queued_followups_instead_of_running_them(tmp_path):
    """Cancelling the active turn re-publishes whatever is still queued behind it to the
    bus, so /stop must empty that queue first or the follow-ups run as fresh turns
    right after "Stopped"."""
    from nanobot.bus.events import InboundMessage
    from nanobot.providers.base import LLMResponse
    from tests.agent.conftest import make_loop, make_provider

    loop = make_loop(tmp_path, provider=make_provider(spec=False))
    seen_users: list[str] = []

    async def chat_with_retry(*, messages, **kwargs):
        user = [m for m in messages if m.get("role") == "user"][-1]["content"]
        seen_users.append(str(user))
        if "slow" in str(user):
            await asyncio.sleep(30)
        return LLMResponse(content="reply", usage={"prompt_tokens": 5, "completion_tokens": 1})

    loop.provider.chat_with_retry = chat_with_retry
    run_task = asyncio.create_task(loop.run())

    async def publish(text: str) -> None:
        await loop.bus.publish_inbound(
            InboundMessage(channel="telegram", chat_id="1", sender_id="u", content=text)
        )

    try:
        await publish("slow please")
        await asyncio.sleep(0.3)
        await publish("follow-1")
        await publish("follow-2")
        await asyncio.sleep(0.2)
        await publish("/stop")
        await asyncio.sleep(0.8)
    finally:
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    assert not any("follow" in user for user in seen_users)


@pytest.mark.asyncio
async def test_run_loop_survives_an_exception_while_routing_one_message(tmp_path):
    """One message whose inline handling raises must not take the whole loop (and so
    the gateway) down; the next message still gets processed."""
    from nanobot.bus.events import InboundMessage
    from nanobot.providers.base import LLMResponse
    from tests.agent.conftest import make_loop, make_provider

    loop = make_loop(tmp_path, provider=make_provider(spec=False))
    calls: list[str] = []

    async def chat_with_retry(*, messages, **kwargs):
        calls.append([m for m in messages if m.get("role") == "user"][-1]["content"])
        return LLMResponse(content="reply", usage={"prompt_tokens": 5, "completion_tokens": 1})

    async def broken_dispatch(ctx):
        raise RuntimeError("handler blew up")

    loop.provider.chat_with_retry = chat_with_retry
    loop.commands.dispatch_priority = broken_dispatch
    run_task = asyncio.create_task(loop.run())

    try:
        await loop.bus.publish_inbound(
            InboundMessage(channel="telegram", chat_id="1", sender_id="u", content="/stop")
        )
        await asyncio.sleep(0.3)
        assert not run_task.done()

        await loop.bus.publish_inbound(
            InboundMessage(channel="telegram", chat_id="1", sender_id="u", content="hello")
        )
        await asyncio.sleep(0.5)
    finally:
        run_task.cancel()
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await run_task

    assert any("hello" in str(c) for c in calls)

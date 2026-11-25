"""One failing outbound message must not stop delivery for every channel (review A03)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections import OrderedDict

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config


class _Stub:
    name = "stub"
    show_reasoning = False
    send_progress = True
    send_tool_hints = False

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message) -> None:
        self.sent.append(message.content)


@pytest.mark.asyncio
async def test_dispatcher_keeps_running_after_an_error_on_one_message() -> None:
    bus = MessageBus()
    manager = ChannelManager.__new__(ChannelManager)
    manager.config = Config()
    manager.bus = bus
    manager.channels = {"stub": _Stub()}
    manager._session_manager = None
    manager._origin_reply_fingerprints = OrderedDict()

    def locked(message, delivered=None):
        if (message.metadata or {}).get("_allocation_delivery"):
            raise sqlite3.OperationalError("database is locked")
        return True

    manager._allocation_delivery = locked
    task = asyncio.create_task(manager._dispatch_outbound())
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="stub", chat_id="1", content="first",
                            metadata={"_allocation_delivery": {"x": 1}})
        )
        await asyncio.sleep(0.2)
        await bus.publish_outbound(OutboundMessage(channel="stub", chat_id="1", content="second"))
        for _ in range(40):
            if manager.channels["stub"].sent:
                break
            await asyncio.sleep(0.1)
        assert not task.done()
        assert manager.channels["stub"].sent == ["second"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

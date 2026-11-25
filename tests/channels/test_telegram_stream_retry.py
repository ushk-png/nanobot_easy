"""A retried stream delta must not appear twice in the final message (review A05)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")
from telegram.error import NetworkError  # noqa: E402

from nanobot.bus.queue import MessageBus  # noqa: E402
from nanobot.channels.telegram import TelegramChannel  # noqa: E402


class _Bot:
    def __init__(self) -> None:
        self.fail_next_send = False
        self.fail_next_edit = False
        self.final: str | None = None

    async def send_message(self, **kwargs):
        if self.fail_next_send:
            self.fail_next_send = False
            raise NetworkError("connection reset")
        self.final = kwargs["text"]
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, **kwargs):
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise NetworkError("connection reset")
        self.final = kwargs["text"]

    async def set_message_reaction(self, **kwargs) -> None:
        return None


def _channel() -> tuple[TelegramChannel, _Bot]:
    channel = TelegramChannel(
        {"enabled": True, "token": "x", "allowFrom": ["1"], "streamEditInterval": 0.1}, MessageBus()
    )
    bot = _Bot()
    channel._app = SimpleNamespace(bot=bot)
    return channel, bot


async def _send_with_retry(channel, delta: str, stream_id: str) -> None:
    for _ in range(3):  # what ChannelManager._send_with_retry does
        try:
            await channel.send_delta("42", delta, stream_id=stream_id)
            return
        except NetworkError:
            await asyncio.sleep(0.12)


@pytest.mark.asyncio
async def test_failed_edit_then_retry_does_not_duplicate_the_delta() -> None:
    channel, bot = _channel()
    await channel.send_delta("42", "Hello ", stream_id="s:0")
    await asyncio.sleep(0.12)
    bot.fail_next_edit = True
    await _send_with_retry(channel, "world.", "s:0")
    await channel.send_delta("42", "", stream_id="s:0", stream_end=True)
    assert bot.final == "Hello world."


@pytest.mark.asyncio
async def test_failed_first_send_then_retry_does_not_duplicate_the_delta() -> None:
    channel, bot = _channel()
    bot.fail_next_send = True
    await _send_with_retry(channel, "Hello ", "s:1")
    await asyncio.sleep(0.12)
    await _send_with_retry(channel, "world.", "s:1")
    await channel.send_delta("42", "", stream_id="s:1", stream_end=True)
    assert bot.final == "Hello world."

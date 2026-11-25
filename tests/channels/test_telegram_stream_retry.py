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


class _LongBot:
    """Records successful sends; can be told to fail the next N send_message calls."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.photos = 0
        self.edits: list[str] = []
        self.fail_sends = 0
        self.edit_not_modified = False

    async def send_message(self, **kwargs):
        if self.fail_sends:
            self.fail_sends -= 1
            raise NetworkError("connection reset")
        self.sent.append(kwargs["text"])
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kwargs):
        from telegram.error import BadRequest

        if self.edit_not_modified:
            raise BadRequest("Message is not modified: specified new message content is the same")
        self.edits.append(kwargs["text"])

    async def send_photo(self, **kwargs):
        self.photos += 1
        return SimpleNamespace(message_id=100 + self.photos)

    send_video = send_voice = send_audio = send_document = send_photo

    async def set_message_reaction(self, **kwargs) -> None:
        return None


def _long_text() -> str:
    return "".join(f"paragraph {index:04d} of the answer.\n" for index in range(400))  # ~15k chars


@pytest.mark.asyncio
async def test_final_stream_retry_still_sends_the_chunks_after_a_failed_extra_send() -> None:
    """Review A06: the retry saw "message is not modified" and dropped the rest of a long answer."""
    channel, _ = _channel()
    bot = _LongBot()
    channel._app = SimpleNamespace(bot=bot)
    text = _long_text()
    await channel.send_delta("42", text, stream_id="s:long")
    preview_sends = len(bot.sent)

    original = bot.send_message
    calls = {"n": 0}

    async def flaky_send(**kwargs):
        calls["n"] += 1
        if calls["n"] in (2, 3):  # second extra chunk: HTML attempt and plain fallback both fail
            raise NetworkError("connection reset")
        return await original(**kwargs)

    bot.send_message = flaky_send
    with pytest.raises(NetworkError):
        await channel.send_delta("42", "", stream_id="s:long", stream_end=True)

    bot.edit_not_modified = True  # the retry's first edit finds the message already updated
    await channel.send_delta("42", "", stream_id="s:long", stream_end=True)

    delivered = bot.sent[preview_sends:]
    assert len(delivered) >= 2
    assert len(delivered) == len(set(delivered)), "no chunk may be sent twice"
    assert "paragraph 0399" in "".join(delivered) or "paragraph 0399" in "".join(bot.edits), (
        "the end of the answer must be delivered"
    )


@pytest.mark.asyncio
async def test_a_retried_send_does_not_repeat_media_or_chunks_that_already_went_out(tmp_path) -> None:
    """Review A07: the manager calls send() again with the same message after a later part failed."""
    from nanobot.bus.events import OutboundMessage

    channel, _ = _channel()
    bot = _LongBot()
    channel._app = SimpleNamespace(bot=bot)
    picture = tmp_path / "chart.png"
    picture.write_bytes(b"\x89PNG" + b"0" * 200)
    message = OutboundMessage(channel="telegram", chat_id="42", content=_long_text(), media=[str(picture)])

    calls = {"n": 0}
    original = bot.send_message

    async def flaky_send(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the second text chunk fails once
            raise NetworkError("connection reset")
        return await original(**kwargs)

    bot.send_message = flaky_send
    with pytest.raises(NetworkError):
        await channel.send(message)
    await channel.send(message)  # what ChannelManager._send_with_retry does

    assert bot.photos == 1
    assert len(bot.sent) == len(set(bot.sent)), "no text chunk may be sent twice"
    assert "paragraph 0399" in "".join(bot.sent)

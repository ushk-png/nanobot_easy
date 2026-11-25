from types import SimpleNamespace

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


class _DummyChannel(BaseChannel):
    name = "dummy"
    _sent: list[OutboundMessage]

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self._sent = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self._sent.append(msg)


def test_is_allowed_requires_exact_match() -> None:
    channel = _DummyChannel(SimpleNamespace(allow_from=["allow@email.com"]), MessageBus())

    assert channel.is_allowed("allow@email.com") is True
    assert channel.is_allowed("attacker|allow@email.com") is False


def test_is_allowed_supports_dict_allow_from_alias() -> None:
    channel = _DummyChannel({"allowFrom": ["alice"]}, MessageBus())

    assert channel.is_allowed("alice") is True


def test_is_allowed_denies_empty_dict_allow_from() -> None:
    channel = _DummyChannel({"allow_from": []}, MessageBus())

    assert channel.is_allowed("alice") is False


def test_is_allowed_handles_none_allow_from() -> None:
    channel = _DummyChannel({"allow_from": None}, MessageBus())
    assert channel.is_allowed("alice") is False

    channel2 = _DummyChannel({"allowFrom": None}, MessageBus())
    assert channel2.is_allowed("alice") is False


def test_is_allowed_star_allows_all() -> None:
    channel = _DummyChannel({"allowFrom": ["*"]}, MessageBus())
    assert channel.is_allowed("anyone") is True


def test_is_allowed_pairing_fallback(monkeypatch) -> None:
    channel = _DummyChannel({"allowFrom": []}, MessageBus())
    monkeypatch.setattr(
        "nanobot.channels.base.is_approved", lambda _ch, sid: sid == "paired"
    )
    assert channel.is_allowed("paired") is True
    assert channel.is_allowed("unknown") is False


@pytest.mark.asyncio
async def test_handle_message_dm_sends_pairing_code(monkeypatch) -> None:
    channel = _DummyChannel({"allowFrom": []}, MessageBus())
    monkeypatch.setattr(
        "nanobot.channels.base.generate_code", lambda _ch, sid: "ABCD-EFGH"
    )

    await channel._handle_message(
        sender_id="stranger", chat_id="chat1", content="hello", is_dm=True
    )

    assert len(channel._sent) == 1
    msg = channel._sent[0]
    assert "ABCD-EFGH" in msg.content
    assert msg.metadata.get("_pairing_code") == "ABCD-EFGH"


@pytest.mark.asyncio
async def test_handle_message_group_ignores_unknown() -> None:
    channel = _DummyChannel({"allowFrom": []}, MessageBus())

    await channel._handle_message(
        sender_id="stranger", chat_id="chat1", content="hello", is_dm=False
    )

    assert channel._sent == []



@pytest.mark.asyncio
async def test_a_stranger_who_keeps_writing_gets_one_pairing_reply_per_minute(monkeypatch) -> None:
    """Review A01: the bot answered every single message of an unknown sender."""
    from nanobot.channels import base as base_module

    monkeypatch.setattr(base_module, "generate_code", lambda channel, sender: "ABCD-EFGH")
    clock = {"now": 100.0}
    monkeypatch.setattr(base_module.time, "monotonic", lambda: clock["now"])
    channel = _DummyChannel({"allow_from": []}, MessageBus())

    for _ in range(5):
        await channel._handle_message(sender_id="stranger", chat_id="c", content="hi", is_dm=True)
    assert len(channel._sent) == 1

    await channel._handle_message(sender_id="someone-else", chat_id="c2", content="hi", is_dm=True)
    assert len(channel._sent) == 2  # another sender is answered right away

    clock["now"] += 61
    await channel._handle_message(sender_id="stranger", chat_id="c", content="hi again", is_dm=True)
    assert len(channel._sent) == 3

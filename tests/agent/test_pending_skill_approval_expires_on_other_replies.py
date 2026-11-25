"""A skill approval question is closed by any other user reply (review C12)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.session.manager import Session
from nanobot.session.skill_approval_state import (
    get_pending_skill_approval,
    set_pending_skill_approval,
)


def _loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = MagicMock()
    return loop


def _session() -> Session:
    session = Session(key="telegram:1")
    set_pending_skill_approval(session.metadata, name="gcalcli-calendar", source="file")
    return session


def _message(content: str, *, channel: str = "telegram", sender: str = "u1") -> InboundMessage:
    return InboundMessage(channel=channel, sender_id=sender, chat_id="1", content=content)


@pytest.mark.asyncio
async def test_an_unrelated_reply_closes_the_pending_approval() -> None:
    loop, session = _loop(), _session()
    result = await loop._resolve_pending_skill_confirmation(
        _message("그건 나중에, 대신 날씨 알려줘"), session, "그건 나중에, 대신 날씨 알려줘"
    )
    assert result is None  # the message goes on to the model as usual
    assert get_pending_skill_approval(session.metadata) is None
    # A later "yes" to some other question now finds nothing to approve.
    assert await loop._resolve_pending_skill_confirmation(_message("네"), session, "네") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,sender", [("system", "cron"), ("telegram", "subagent")])
async def test_automatic_messages_do_not_close_the_pending_approval(channel, sender) -> None:
    loop, session = _loop(), _session()
    await loop._resolve_pending_skill_confirmation(
        _message("background job finished", channel=channel, sender=sender), session,
        "background job finished",
    )
    assert get_pending_skill_approval(session.metadata) is not None

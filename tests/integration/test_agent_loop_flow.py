"""The real AgentLoop + MessageBus with a scripted provider: what a Telegram user experiences."""

from __future__ import annotations

import asyncio
import contextlib

from nanobot.bus.events import InboundMessage
from nanobot.providers.base import LLMResponse
from tests.agent.conftest import make_loop, make_provider


class Gateway:
    """Boots the real run() loop; ``script`` decides each model reply from the user's text."""

    def __init__(self, tmp_path, script):
        self.loop = make_loop(tmp_path, provider=make_provider(spec=False))
        self.seen: list[str] = []

        async def chat_with_retry(*, messages, **_kwargs):
            user = str([m for m in messages if m.get("role") == "user"][-1]["content"])
            self.seen.append(user)
            return await script(user)

        self.loop.provider.chat_with_retry = chat_with_retry
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> Gateway:
        self.task = asyncio.create_task(self.loop.run())
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.task is not None
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task

    async def say(self, text: str, chat: str = "1") -> None:
        await self.loop.bus.publish_inbound(
            InboundMessage(channel="telegram", chat_id=chat, sender_id="u", content=text)
        )

    async def reply(self, chat: str = "1", timeout: float = 5.0) -> str:
        """Next non-empty message addressed to ``chat``."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            msg = await asyncio.wait_for(self.loop.bus.consume_outbound(), max(remaining, 0.01))
            if msg.chat_id == chat and (msg.content or "").strip():
                return msg.content


def _answer(text: str) -> LLMResponse:
    return LLMResponse(content=text, usage={"prompt_tokens": 5, "completion_tokens": 1})


async def test_user_message_gets_a_reply(tmp_path) -> None:
    async def script(user):
        return _answer("hi there")

    async with Gateway(tmp_path, script) as gw:
        await gw.say("hello")
        assert await gw.reply() == "hi there"


async def test_a_failing_model_call_does_not_break_the_next_message(tmp_path) -> None:
    async def script(user):
        if "first" in user:
            raise RuntimeError("provider exploded")
        return _answer("second ok")

    async with Gateway(tmp_path, script) as gw:
        await gw.say("first")
        assert await gw.reply()  # the user is told something went wrong, not left hanging

        await gw.say("second")
        assert await gw.reply() == "second ok"
        assert not gw.task.done()


async def test_stop_interrupts_a_slow_turn_and_the_next_message_still_works(tmp_path) -> None:
    async def script(user):
        if "slow" in user:
            await asyncio.sleep(60)
        return _answer("fast reply")

    async with Gateway(tmp_path, script) as gw:
        await gw.say("slow please")
        await asyncio.sleep(0.3)  # let the slow turn start
        await gw.say("/stop")
        assert "Stopped" in await gw.reply()

        await gw.say("hello again")
        assert await gw.reply() == "fast reply"


async def test_a_slow_chat_does_not_block_another_chat(tmp_path) -> None:
    release = asyncio.Event()

    async def script(user):
        if "slow" in user:
            await release.wait()
            return _answer("slow done")
        return _answer("fast done")

    async with Gateway(tmp_path, script) as gw:
        await gw.say("slow one", chat="A")
        await asyncio.sleep(0.2)
        await gw.say("quick one", chat="B")
        assert await gw.reply(chat="B") == "fast done"

        release.set()
        assert await gw.reply(chat="A") == "slow done"

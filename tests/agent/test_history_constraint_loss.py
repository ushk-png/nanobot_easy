"""Default path (no opt-in): where an early user constraint leaves the replayed prompt.

These pass before and after the opt-in change; they pin the problem and the unchanged default.
Offline: a scripted fake provider records the prompts it receives.
"""

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse

CONSTRAINT = "이번 작업에서 production.json은 절대로 변경하지 마. sample.json만 변경할 수 있어."
KEY = "cli:loss"


class Fake:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.provider = MagicMock()
        self.provider.get_default_model.return_value = "test-model"
        self.provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
        self.provider.chat_with_retry = self.chat
        self.provider.chat_stream_with_retry = self.chat

    async def chat(self, **kwargs):
        self.calls.append({"tools": bool(kwargs.get("tools")), "messages": copy.deepcopy(kwargs["messages"])})
        if not kwargs.get("tools"):
            return LLMResponse(content="(summary of earlier turns)", tool_calls=[], usage={})
        return LLMResponse(content=self.replies.pop(0) if self.replies else "done", tool_calls=[], usage={})

    def last_turn_text(self):
        messages = [c["messages"] for c in self.calls if c["tools"]][-1]
        return json.dumps(messages, ensure_ascii=False)


def make_loop(tmp_path, fake):
    return AgentLoop(bus=MessageBus(), provider=fake.provider, workspace=tmp_path, model="test-model",
                     context_window_tokens=200_000)


async def converse(loop, texts):
    for text in texts:
        await loop.process_direct(text, session_key=KEY, chat_id="loss")


@pytest.mark.asyncio
async def test_constraint_is_replayed_while_history_is_short(tmp_path):
    fake = Fake(["알겠습니다."])
    await converse(make_loop(tmp_path, fake), [CONSTRAINT, "sample.json을 바꿔줘."])
    assert "production.json은 절대로 변경하지 마" in fake.last_turn_text()


@pytest.mark.asyncio
async def test_token_cap_drops_the_constraint(tmp_path):
    fake = Fake(["알겠습니다.", "분석 자료 " * 9000, "분석을 마쳤습니다."])
    await converse(make_loop(tmp_path, fake),
                   [CONSTRAINT, "먼저 분석 내용을 정리해줘.", "분석이 끝났는지만 알려줘.", "sample.json을 바꿔줘."])
    assert "production.json은 절대로 변경하지 마" not in fake.last_turn_text()


@pytest.mark.asyncio
async def test_message_cap_and_replay_window_consolidation_drop_the_constraint(tmp_path):
    fake = Fake(["알겠습니다."] + ["응."] * 8)
    loop = make_loop(tmp_path, fake)
    # Force a tight replay *token* budget (what actually gates overflow consolidation
    # now) rather than the old, no-longer-consulted message-count override.
    loop.history_context_tokens = 80
    await converse(loop, [CONSTRAINT] + [f"질문 {i}" for i in range(8)])
    assert loop.sessions.get_or_create(KEY).last_consolidated > 0
    assert any(not c["tools"] for c in fake.calls)
    # The summary is model-written text; the user's own words are no longer in the prompt.
    assert "production.json은 절대로 변경하지 마" not in fake.last_turn_text()


@pytest.mark.asyncio
async def test_idle_compaction_deletes_the_constraint_row(tmp_path):
    fake = Fake(["알겠습니다.", "응.", "응.", "응."])
    loop = make_loop(tmp_path, fake)
    await converse(loop, [CONSTRAINT, "질문 1", "질문 2"])
    await loop.consolidator.compact_idle_session(KEY, max_suffix=2)
    assert all(m.get("content") != CONSTRAINT for m in loop.sessions.get_or_create(KEY).messages)
    await converse(loop, ["sample.json을 바꿔줘."])
    assert "production.json은 절대로 변경하지 마" not in fake.last_turn_text()

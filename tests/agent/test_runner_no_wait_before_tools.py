"""The task-note path must not park a turn until a running sub-agent reports (review C3-03)."""

from __future__ import annotations

import asyncio
import time

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse, ToolCallRequest


class _Provider:
    generation = type("G", (), {"max_tokens": 1024})()

    def __init__(self) -> None:
        self.calls = 0

    def get_default_model(self) -> str:
        return "fake"

    async def chat_with_retry(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="", finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "notes.md"})],
            )
        return LLMResponse(content="done", finish_reason="stop")


class _TrackedRead(ReadFileTool):
    ran = False

    async def execute(self, *args, **kwargs):
        type(self).ran = True
        return await super().execute(*args, **kwargs)


@pytest.mark.asyncio
async def test_tools_run_without_waiting_for_a_running_subagent(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("hello\n")
    _TrackedRead.ran = False
    tools = ToolRegistry()
    tools.register(_TrackedRead(workspace=tmp_path))
    waits_before_tools: list[float] = []

    async def drain_pending(*, limit: int = 3, wait: bool = True):
        # Same rule as AgentLoop._drain_pending: with wait=True and a sub-agent still running
        # it blocks (up to 300 s in production). Waiting after the final answer is intended;
        # waiting before the model's tool calls have run is the defect.
        if wait and not _TrackedRead.ran:
            waits_before_tools.append(time.monotonic())
            await asyncio.sleep(30)
        return []

    started = time.monotonic()
    result = await asyncio.wait_for(
        AgentRunner(_Provider()).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "notes.md 검토해줘"}],
            tools=tools, model="fake", max_iterations=5, max_tool_result_chars=4000,
            workspace=tmp_path, session_key="telegram:42", concurrent_tools=True,
            injection_callback=drain_pending, task_note_enabled=True, task_note_metadata={},
        )),
        timeout=15,
    )
    assert waits_before_tools == []
    assert time.monotonic() - started < 10
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_messages and "Superseded" not in str(tool_messages[0]["content"])

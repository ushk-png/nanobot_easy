"""Tools run through the task-note wrapper must keep their per-turn ContextVar writes (review C3-02).

The message tool records "already delivered to this chat in this turn" in ContextVars; the loop
reads them to avoid sending the same reply a second time. A child task works on a copy of the
context, so the record used to disappear and the answer went out twice.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from nanobot.agent.task_note import TaskNote, bounded_operation
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.message import MessageTool

_marker: contextvars.ContextVar[str] = contextvars.ContextVar("_marker", default="unset")


@pytest.mark.asyncio
async def test_message_tool_delivery_record_survives_the_task_note_wrapper(tmp_path) -> None:
    sent: list[object] = []

    async def send(message) -> None:
        sent.append(message)

    tool = MessageTool(send_callback=send, workspace=tmp_path)
    tool.set_context(
        RequestContext(channel="telegram", chat_id="42", message_id="m1",
                       session_key="telegram:42", metadata={})
    )
    tool.start_turn()
    note = TaskNote([{"role": "user", "content": "notes.md 검토해줘"}], tmp_path, {})

    await bounded_operation(note, "message", lambda: tool.execute(content="report attached"))

    assert len(sent) == 1
    assert tool._sent_in_turn is True
    assert tool.turn_delivered_contents() == ["report attached"]


@pytest.mark.asyncio
async def test_child_context_changes_are_handed_back_only_on_success(tmp_path) -> None:
    note = TaskNote([{"role": "user", "content": "notes.md 검토해줘"}], tmp_path, {})

    async def sets_marker() -> str:
        _marker.set("from-child")
        return "ok"

    assert await bounded_operation(note, "read_file", sets_marker) == "ok"
    assert _marker.get() == "from-child"

    _marker.set("before")

    async def slow() -> str:
        _marker.set("late")
        await asyncio.sleep(5)
        return "never"

    result = await bounded_operation(note, "read_file", slow, timeout=0.05, grace=0.05)
    assert "Tool timeout" in str(result)
    assert _marker.get() == "before"  # a timed-out operation changes nothing

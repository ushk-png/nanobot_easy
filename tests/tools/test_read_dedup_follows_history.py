"""A "file unchanged" stub must not outlive the history it points at (review C03)."""

from __future__ import annotations

import pytest

from nanobot.agent.loop import AgentLoop  # noqa: F401  (import check: loop wires the epoch)
from nanobot.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.session.manager import Session


def test_store_starts_fresh_when_the_epoch_changes_and_keeps_one_state_per_session() -> None:
    store = FileStateStore()
    first = store.for_session("telegram:1", (0, 0))
    assert store.for_session("telegram:1", (0, 0)) is first
    second = store.for_session("telegram:1", (1, 0))
    assert second is not first
    assert store.for_session("telegram:1", (1, 0)) is second
    assert len(store._states_by_key) == 1  # the old state was dropped, memory stays bounded
    assert store.for_session("telegram:2", (0, 0)) is not second


def test_history_epoch_moves_when_earlier_messages_leave_the_context() -> None:
    session = Session(key="telegram:1")
    for index in range(6):
        session.add_message("user" if index % 2 == 0 else "assistant", f"m{index}")
    epoch = session.history_epoch
    session.retain_recent_legal_suffix(2)
    assert session.history_epoch > epoch
    epoch = session.history_epoch
    session.clear()
    assert session.history_epoch > epoch


@pytest.mark.asyncio
async def test_a_repeated_read_returns_content_again_after_new_or_compaction(tmp_path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("hello world\n")
    tool = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    store = FileStateStore()
    session = Session(key="telegram:1")

    async def read_once() -> str:
        token = bind_file_states(store.for_session(session.key, (session.history_epoch, session.last_consolidated)))
        try:
            return str(await tool.execute(path=str(target)))
        finally:
            reset_file_states(token)

    first = await read_once()
    assert "hello world" in first
    assert "File unchanged" in await read_once()  # same history: the stub is still fair
    session.clear()  # /new
    again = await read_once()
    assert "hello world" in again and "File unchanged" not in again

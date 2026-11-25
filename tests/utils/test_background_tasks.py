"""Fire-and-forget tasks are kept alive and failures are logged (review: unreferenced tasks, A04, A16)."""

from __future__ import annotations

import asyncio
import gc

import pytest
from loguru import logger

from nanobot.utils import background
from nanobot.utils.background import spawn_background, track_task


@pytest.mark.asyncio
async def test_a_spawned_task_survives_garbage_collection_and_is_released_when_done() -> None:
    finished = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0.05)
        finished.set()

    spawn_background(work(), name="probe")  # the handle is dropped on purpose
    gc.collect()
    await asyncio.wait_for(finished.wait(), timeout=2)
    await asyncio.sleep(0.05)
    assert not background._BACKGROUND_TASKS  # nothing is held after it finished


@pytest.mark.asyncio
async def test_a_failing_background_task_is_logged() -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="ERROR")
    try:
        async def boom() -> None:
            raise RuntimeError("kaboom")

        task = spawn_background(boom(), name="boom-task")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.05)
    finally:
        logger.remove(sink)
    assert any("boom-task" in line for line in lines)


@pytest.mark.asyncio
async def test_track_task_ignores_cancellation() -> None:
    task = track_task(asyncio.create_task(asyncio.sleep(10)), name="sleeper")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.02)
    assert task not in background._BACKGROUND_TASKS

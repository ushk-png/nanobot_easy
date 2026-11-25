"""Keep fire-and-forget asyncio tasks alive and make their failures visible."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from loguru import logger

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def track_task(task: asyncio.Task[Any], *, name: str = "") -> asyncio.Task[Any]:
    """Hold a strong reference until ``task`` ends and log an exception nobody else will see.

    The event loop keeps only weak references to tasks. A task whose handle is dropped can be
    garbage collected while it is still running, which silently abandons the work (a restart
    notice, a Dream run, a skill draft), and an exception it raises is never reported.
    """
    _BACKGROUND_TASKS.add(task)

    def _finished(done: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.opt(exception=error).error("Background task {} failed", name or done.get_name())

    task.add_done_callback(_finished)
    return task


def spawn_background(coro: Coroutine[Any, Any, Any], *, name: str = "") -> asyncio.Task[Any]:
    """``asyncio.create_task`` for work whose result nobody awaits."""
    return track_task(asyncio.create_task(coro, name=name or None), name=name)

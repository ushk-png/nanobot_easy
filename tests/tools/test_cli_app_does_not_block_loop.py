"""Running a CLI App must not freeze the event loop of the whole gateway (review C15)."""

from __future__ import annotations

import asyncio
import time

import pytest

from nanobot.agent.tools.cli_apps import CliAppsTool


@pytest.mark.asyncio
async def test_a_slow_cli_app_does_not_stall_other_tasks(tmp_path, monkeypatch) -> None:
    class _SlowManager:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def installed_names(self):
            return ["sleepy"]

        def run(self, name, **kwargs) -> str:
            time.sleep(0.8)  # a blocking subprocess
            return "done"

    monkeypatch.setattr("nanobot.agent.tools.cli_apps.CliAppManager", _SlowManager)
    gaps: list[float] = []

    async def ticker() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    result = await CliAppsTool(workspace=tmp_path).execute(name="sleepy")
    task.cancel()
    assert result == "done"
    assert gaps and max(gaps) < 0.3, f"the event loop stalled for {max(gaps):.2f}s"

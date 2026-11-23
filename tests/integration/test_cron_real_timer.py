"""CronService with real timers and a real store file: reminders for a single user."""

from __future__ import annotations

import asyncio
import time

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronSchedule

DAY_MS = 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _chat() -> dict[str, str]:
    return {"session_key": "telegram:1", "origin_channel": "telegram", "origin_chat_id": "1"}


class Recorder:
    def __init__(self) -> None:
        self.fired: list[str] = []

    async def __call__(self, job: CronJob) -> None:
        self.fired.append(job.name)


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _add_at(service: CronService, name: str, at_ms: int) -> CronJob:
    return service.add_job(
        name=name, schedule=CronSchedule(kind="at", at_ms=at_ms), message="ping", **_chat(),
    )


async def test_one_shot_reminder_fires_when_due(tmp_path) -> None:
    recorder = Recorder()
    service = CronService(tmp_path / "jobs.json", on_job=recorder)
    _add_at(service, "due-soon", _now_ms() + 100)

    await service.start()
    try:
        assert await _wait_until(lambda: recorder.fired == ["due-soon"])
    finally:
        service.stop()


async def test_reminder_that_came_due_while_the_service_was_down_fires_after_restart(tmp_path) -> None:
    path = tmp_path / "jobs.json"
    _add_at(CronService(path, on_job=Recorder()), "missed", _now_ms() + 100)
    await asyncio.sleep(0.3)  # the gateway is down; the reminder's time passes

    recorder = Recorder()
    restarted = CronService(path, on_job=recorder)
    await restarted.start()
    try:
        assert await _wait_until(lambda: recorder.fired == ["missed"])
    finally:
        restarted.stop()


async def test_long_stale_reminders_are_not_replayed(tmp_path) -> None:
    """Only recently missed reminders are caught up; an old backlog must not fire all at once."""
    path = tmp_path / "jobs.json"
    _add_at(CronService(path, on_job=Recorder()), "ancient", _now_ms() - 3 * DAY_MS)

    recorder = Recorder()
    restarted = CronService(path, on_job=recorder)
    await restarted.start()
    try:
        await asyncio.sleep(0.4)
        assert recorder.fired == []
    finally:
        restarted.stop()

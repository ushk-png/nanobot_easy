"""A transient error must neither hide the triggers nor stop the queue (review B04, B05)."""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import pytest

from nanobot.triggers import local_runner
from nanobot.triggers.local_store import LocalTriggerStore, TriggerStoreError


def test_a_transient_read_error_keeps_the_store_in_place(tmp_path, monkeypatch) -> None:
    store = LocalTriggerStore(tmp_path)
    trigger = store.create(name="t", channel="websocket", chat_id="c", session_key="websocket:c")
    real_read = Path.read_text
    failures = {"left": 1}

    def flaky(self, *args, **kwargs):
        if self == store.store_path and failures["left"]:
            failures["left"] -= 1
            raise OSError(errno.EMFILE, "Too many open files")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    with pytest.raises(TriggerStoreError):
        store.get(trigger.id)
    monkeypatch.setattr(Path, "read_text", real_read)

    assert store.get(trigger.id) is not None  # still there on the next call
    assert not list(store.root.glob("triggers.json.corrupt-*"))
    store.enqueue(trigger.id, "hello")  # and deliveries are not rejected as "not found"


def test_a_file_that_is_not_json_is_still_set_aside(tmp_path) -> None:
    store = LocalTriggerStore(tmp_path)
    store.create(name="t", channel="websocket", chat_id="c", session_key="websocket:c")
    store.store_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(TriggerStoreError):
        store.list_triggers()
    assert list(store.root.glob("triggers.json.corrupt-*"))


@pytest.mark.asyncio
async def test_the_queue_task_survives_store_errors(tmp_path, monkeypatch) -> None:
    store = LocalTriggerStore(tmp_path)
    calls = {"n": 0}
    real_claim = store.claim_deliveries

    def flaky_claim(*, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_claim(limit=limit)

    monkeypatch.setattr(store, "claim_deliveries", flaky_claim)

    async def submit_turn(*args, **kwargs):
        return None

    task = asyncio.create_task(
        local_runner.run_local_trigger_queue(store=store, submit_turn=submit_turn, poll_interval_s=0.02)
    )
    try:
        await asyncio.sleep(0.3)
        assert not task.done(), "one store error ended the queue task (and with it the gateway)"
        assert calls["n"] >= 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

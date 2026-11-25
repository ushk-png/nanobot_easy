from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.webui.token_usage import (
    TokenUsageHook,
    record_response_token_usage,
    record_token_usage,
    token_usage_payload,
)


def test_record_token_usage_aggregates_by_local_day(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    record_token_usage(
        {"prompt_tokens": 100, "completion_tokens": 40, "cached_tokens": 20},
        timezone_name="Asia/Shanghai",
        now=datetime(2026, 6, 2, 18, 0, tzinfo=timezone.utc),
    )
    record_token_usage(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        timezone_name="Asia/Shanghai",
        now=datetime(2026, 6, 2, 19, 0, tzinfo=timezone.utc),
    )

    payload = token_usage_payload(
        timezone_name="Asia/Shanghai",
        now=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
    )

    assert payload["total_tokens_30d"] == 155
    assert payload["active_days_30d"] == 1
    assert payload["requests_30d"] == 2
    assert payload["days"] == [
        {
            "date": "2026-06-03",
            "prompt_tokens": 110,
            "completion_tokens": 45,
            "cached_tokens": 20,
            "total_tokens": 155,
            "provider_tokens": 155,
            "estimated_tokens": 0,
            "requests": 2,
            "provider_requests": 2,
            "estimated_requests": 0,
            "sources": {
                "user": {
                    "prompt_tokens": 110,
                    "completion_tokens": 45,
                    "cached_tokens": 20,
                    "total_tokens": 155,
                    "provider_tokens": 155,
                    "estimated_tokens": 0,
                    "requests": 2,
                    "provider_requests": 2,
                    "estimated_requests": 0,
                }
            },
        }
    ]


def test_record_token_usage_skips_empty_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    record_token_usage({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    payload = token_usage_payload(now=datetime(2026, 6, 3, tzinfo=timezone.utc))
    assert payload["days"] == []
    assert payload["total_tokens_30d"] == 0


def test_record_token_usage_keeps_estimated_split(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    record_token_usage(
        {"prompt_tokens": 100, "completion_tokens": 25, "estimated_tokens": 125},
        now=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    payload = token_usage_payload(now=datetime(2026, 6, 3, tzinfo=timezone.utc))

    assert payload["days"][0]["total_tokens"] == 125
    assert payload["days"][0]["provider_tokens"] == 0
    assert payload["days"][0]["estimated_tokens"] == 125
    assert payload["days"][0]["estimated_requests"] == 1


def test_record_token_usage_keeps_source_breakdown(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    record_token_usage(
        {"prompt_tokens": 100, "completion_tokens": 25},
        source="user",
        now=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    record_token_usage(
        {"prompt_tokens": 20, "completion_tokens": 5},
        source="dream",
        now=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    payload = token_usage_payload(now=datetime(2026, 6, 3, tzinfo=timezone.utc))
    row = payload["days"][0]

    assert row["total_tokens"] == 150
    assert row["sources"]["user"]["total_tokens"] == 125
    assert row["sources"]["user"]["requests"] == 1
    assert row["sources"]["dream"]["total_tokens"] == 25
    assert row["sources"]["dream"]["requests"] == 1


def test_record_response_token_usage_uses_response_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")
    monkeypatch.setattr("nanobot.webui.token_usage._local_day", lambda *_, **__: "2026-06-03")

    record_response_token_usage(
        SimpleNamespace(usage={"prompt_tokens": 20, "completion_tokens": 5}),
        source="dream",
    )

    payload = token_usage_payload(now=datetime(2026, 6, 3, tzinfo=timezone.utc))
    assert payload["days"][0]["sources"]["dream"]["total_tokens"] == 25


@pytest.mark.asyncio
async def test_token_usage_hook_classifies_source_from_session_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")
    monkeypatch.setattr("nanobot.webui.token_usage._local_day", lambda *_, **__: "2026-06-03")

    hook = TokenUsageHook()
    await hook.after_iteration(
        AgentHookContext(
            iteration=0,
            messages=[],
            session_key="cron:drink-water",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
    )

    payload = token_usage_payload(now=datetime(2026, 6, 3, tzinfo=timezone.utc))

    assert payload["days"][0]["sources"]["cron"]["total_tokens"] == 15


def _record_many(webui_dir: str, count: int, queue) -> None:
    from pathlib import Path

    from nanobot.webui import token_usage

    token_usage.get_webui_dir = lambda: Path(webui_dir)
    failures = 0
    for _ in range(count):
        try:
            token_usage.record_token_usage({"prompt_tokens": 10, "completion_tokens": 0})
        except Exception:
            failures += 1
    queue.put(failures)


def test_several_gateway_processes_do_not_lose_or_corrupt_records(tmp_path) -> None:
    """Review A31: six gateways share token-usage.json; 565 of 900 records were lost."""
    import json
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    processes, each = 4, 40
    queue = ctx.Queue()
    workers = [ctx.Process(target=_record_many, args=(str(tmp_path), each, queue)) for _ in range(processes)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    assert sum(queue.get(timeout=5) for _ in workers) == 0
    data = json.loads((tmp_path / "token-usage.json").read_text())
    assert sum(day["requests"] for day in data["days"].values()) == processes * each
    assert not list(tmp_path.glob("token-usage.json.*.tmp"))


def test_unreadable_state_file_is_not_replaced_by_an_empty_history(tmp_path, monkeypatch) -> None:
    import builtins
    import errno
    import json

    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path)
    record_token_usage({"prompt_tokens": 10, "completion_tokens": 0})
    path = tmp_path / "token-usage.json"
    before = path.read_text()
    real_open = builtins.open

    def failing(file, *args, **kwargs):
        if str(file).endswith("token-usage.json") and "w" not in (args[0] if args else "r"):
            raise OSError(errno.EIO, "I/O error")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing)
    record_token_usage({"prompt_tokens": 5, "completion_tokens": 0})  # skipped, not a reset
    monkeypatch.setattr(builtins, "open", real_open)
    assert path.read_text() == before
    assert json.loads(before)["days"]


def test_invalid_json_state_is_kept_aside(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.webui.token_usage.get_webui_dir", lambda: tmp_path)
    (tmp_path / "token-usage.json").write_text("{half", encoding="utf-8")
    record_token_usage({"prompt_tokens": 7, "completion_tokens": 0})
    kept = list(tmp_path.glob("token-usage.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text() == "{half"

"""Phase-1 usage measurement: per-attempt records, no double counting, no behaviour change."""

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from nanobot.providers import usage_ledger
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.fallback_provider import FallbackProvider

SECRET = "PROMPT-SECRET-7f3a"
ANSWER = "ANSWER-SECRET-91bc"


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def refuse(*_a, **_k):
        raise RuntimeError("unexpected network access in usage ledger test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def ledger(monkeypatch, tmp_path):
    fresh = usage_ledger._Ledger()
    monkeypatch.setattr(usage_ledger, "ledger", fresh)
    fresh.configure(tmp_path / "usage", tmp_path / "workspace-test")
    yield fresh
    fresh.flush(2)


def file_records(ledger):
    assert ledger.flush(2)
    return usage_ledger.read_records(ledger.path)


class Fake(LLMProvider):
    def __init__(self, steps, model="fake-model", deltas=0, delay=0.0):
        super().__init__()
        self.steps, self.model, self.deltas, self.delay = list(steps), model, deltas, delay
        self.seen = []

    def get_default_model(self):
        return self.model

    async def _next(self, kwargs):
        from nanobot.agent.context_trace import active_context_trace
        self.seen.append((usage_ledger.current_scope(), active_context_trace.get()))
        if self.delay:
            await asyncio.sleep(self.delay)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step()
        return step

    async def chat(self, **kwargs):
        return await self._next(kwargs)

    async def chat_stream(self, **kwargs):
        for _ in range(self.deltas):
            if kwargs.get("on_content_delta"):
                await kwargs["on_content_delta"]("x")
        return await self._next(kwargs)


def ok(usage=None, content=ANSWER):
    return LLMResponse(content=content, usage=usage or {})


MSG = [{"role": "user", "content": SECRET}]


async def test_non_stream_and_stream_success_are_summed_exactly_once(ledger):
    provider = Fake([ok({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}),
                     ok({"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230})], deltas=50)
    with usage_ledger.bound(usage_ledger.request_scope("conversation", workspace="w", request_key="t1")):
        assert (await provider.chat_with_retry(messages=MSG)).content == ANSWER
        chunks = []

        async def on_delta(text):
            chunks.append(text)
        response = await provider.chat_stream_with_retry(messages=MSG, on_content_delta=on_delta)
    assert response.content == ANSWER and len(chunks) == 50
    records = file_records(ledger)
    assert len(records) == 2                      # one line per attempt, not per stream chunk
    assert [r["stream"] for r in records] == [False, True]
    row = usage_ledger.summarize(records)["conversation"]
    assert row["logical_requests"] == 2 and row["attempts"] == 2
    assert row["measured"]["total_tokens"] == 350 and row["measured"]["input_tokens"] == 300
    assert row["unknown_usage_attempts"] == 0 and row["estimated_attempts"] == 0
    dumped = ledger.path.read_text()
    assert SECRET not in dumped and ANSWER not in dumped and "t1" not in dumped


async def test_retry_failures_are_attempts_of_one_logical_request_and_keep_usage(ledger, monkeypatch):
    async def no_sleep(_delay):
        return None
    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", no_sleep)
    provider = Fake([
        LLMResponse(content="503 server error", finish_reason="error",
                    usage={"prompt_tokens": 50, "completion_tokens": 0, "total_tokens": 50}),
        LLMResponse(content="429 rate limit", finish_reason="error"),
        ok({"prompt_tokens": 60, "completion_tokens": 10, "total_tokens": 70}),
    ])
    await provider.chat_with_retry(messages=MSG)
    records = file_records(ledger)
    assert [r["status"] for r in records] == ["failed", "failed", "succeeded"]
    assert len({r["logical_call_id"] for r in records}) == 1
    assert [r["attempt_index"] for r in records] == [1, 2, 3]
    assert [r["usage_status"] for r in records] == ["measured", "unknown", "measured"]
    row = usage_ledger.summarize(records)["unattributed"]
    assert row["measured"]["total_tokens"] == 120 and row["unknown_usage_attempts"] == 1
    assert row["logical_requests"] == 1 and row["attempts"] == 3


async def test_fallback_records_each_leaf_attempt_once_and_not_the_wrapper(ledger):
    primary = Fake([LLMResponse(content="server error", finish_reason="error", error_kind="server_error",
                                error_status_code=503,
                                usage={"prompt_tokens": 40, "completion_tokens": 0, "total_tokens": 40})],
                   model="primary-model")
    backup = Fake([ok({"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45})], model="backup-model")
    preset = SimpleNamespace(model="backup-model", max_tokens=100, temperature=0.0, reasoning_effort=None)
    wrapper = FallbackProvider(primary, [preset], lambda _p: backup)
    response = await wrapper.chat_with_retry(messages=MSG, model="primary-model")
    assert response.finish_reason == "stop"
    records = file_records(ledger)
    assert [(r["model"], r["status"]) for r in records] == [("primary-model", "failed"),
                                                           ("backup-model", "succeeded")]
    assert all(r["provider"] == "Fake" for r in records)
    assert len({r["logical_call_id"] for r in records}) == 1
    assert usage_ledger.summarize(records)["unattributed"]["measured"]["total_tokens"] == 85


async def test_missing_or_zero_usage_is_never_zero_tokens(ledger):
    provider = Fake([ok({}), ok({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
                     LLMResponse(content="bad request", finish_reason="error", error_status_code=400)])
    await provider.chat_with_retry(messages=MSG)
    await provider.chat_with_retry(messages=MSG)
    await provider.chat_with_retry(messages=MSG, max_retries=0)
    records = file_records(ledger)
    assert [r["usage_status"] for r in records] == ["estimated", "estimated", "unknown"]
    assert all(r.get("total_tokens") is None for r in records)
    row = usage_ledger.summarize(records)["unattributed"]
    assert row["measured_attempts"] == 0 and row["measured"]["total_tokens"] is None
    assert row["estimated_attempts"] == 2 and row["estimated_total_tokens"] > 0
    assert row["unknown_usage_attempts"] == 1


def test_cache_and_reasoning_are_breakdowns_not_added_to_total():
    anthropic = usage_ledger.normalize_usage({"prompt_tokens": 1000, "completion_tokens": 200,
                                              "total_tokens": 1200, "cached_tokens": 800,
                                              "cache_read_input_tokens": 800,
                                              "cache_creation_input_tokens": 100})
    assert anthropic["total_tokens"] == 1200 and anthropic["cache_read_tokens"] == 800
    assert anthropic["cache_write_tokens"] == 100
    responses = usage_ledger.normalize_usage({"prompt_tokens": 500, "completion_tokens": 300,
                                              "reasoning_tokens": 250, "cached_tokens": 0})
    assert responses["total_tokens"] == 800 and responses["total_source"] == "input_plus_output"
    assert responses["reasoning_tokens"] == 250 and responses["cache_read_tokens"] == 0
    rows = usage_ledger.summarize([{"usage_status": "measured", "logical_call_id": "a", **anthropic},
                                   {"usage_status": "measured", "logical_call_id": "b", **responses}])
    assert rows["unknown"]["measured"]["total_tokens"] == 2000


def test_partial_usage_does_not_turn_missing_tokens_into_zero():
    partial = usage_ledger.normalize_usage({"prompt_tokens": 100})
    assert partial["input_tokens"] == 100
    assert partial["output_tokens"] is None
    assert partial["total_tokens"] is None
    assert partial["total_source"] == "unknown"
    assert usage_ledger.normalize_usage({"prompt_tokens": 100, "total_tokens": 0})["total_tokens"] is None
    complete = usage_ledger.normalize_usage({"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 0})
    assert complete["total_tokens"] == 100 and complete["total_source"] == "input_plus_output"
    row = usage_ledger.summarize([
        {"usage_status": "measured", "logical_call_id": "partial", **partial}
    ])["unknown"]
    assert row["measured"]["input_tokens"] == 100
    assert row["measured"]["output_tokens"] is None
    assert row["measured"]["total_tokens"] is None
    assert "total_tokens" not in row["measured_field_reported_attempts"]


async def test_gateway_usage_directory_failure_keeps_provider_working(ledger, monkeypatch):
    def inaccessible(_name):
        raise PermissionError("cannot create telemetry directory")

    monkeypatch.setattr("nanobot.config.paths.get_runtime_subdir", inaccessible)
    assert usage_ledger.configure_gateway("workspace") is None
    provider = Fake([ok({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})])
    assert (await provider.chat_with_retry(messages=MSG)).content == ANSWER
    assert ledger.path is None
    assert list(ledger.recent)[-1]["total_tokens"] == 3


async def test_concurrent_conversation_and_memory_refresh_stay_separate(ledger, tmp_path, monkeypatch):
    from nanobot.agent.context_trace import ContextTrace, active_context_trace
    from nanobot.agent.memory_prompt import make_snapshot
    from nanobot.agent.memory_prompt_refresh import MemoryPromptRefresher
    from nanobot.agent.task_budget import TaskBudget
    from tests.agent.test_memory_prompt_refresh import APPROVED, REVISED, RULE, draft

    monkeypatch.setattr("nanobot.agent.memory_prompt_refresh._DEBOUNCE_S", 0)
    (tmp_path / "memory").mkdir()
    source = tmp_path / "memory" / "MEMORY.md"
    old = RULE + "\n" + "Old implementation detail.\n" * 400
    source.write_text(old)
    source.with_name("prompt_snapshot.json").write_text(json.dumps(make_snapshot(old, RULE, source, 240)))
    text = old.replace("Old implementation detail.", "Revised detail.", 1) + "\n" + REVISED
    source.write_text(text)
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    memory_model = Fake([draft(REVISED), LLMResponse(content=APPROVED, usage=usage)], delay=0.01)
    memory_model.steps[0].usage = dict(usage)
    refresher = MemoryPromptRefresher(source, 240, lambda: (memory_model, "memory-model"))
    chat = Fake([ok(usage) for _ in range(4)], delay=0.01)
    trace = ContextTrace("chat-request")
    trace.budget = TaskBudget("chat-request", max_sends=50)
    chat_scope = usage_ledger.request_scope("conversation", workspace=tmp_path, request_key="turn-1")

    async def conversation():
        with usage_ledger.bound(chat_scope):
            token = active_context_trace.set(trace)
            try:
                refresher.request(text)             # triggered from the chat turn
                for _ in range(4):
                    await chat.chat_with_retry(messages=MSG)
            finally:
                active_context_trace.reset(token)

    await conversation()
    await asyncio.wait_for(refresher._task, 5)
    await refresher.close()
    records = file_records(ledger)
    by_purpose = usage_ledger.summarize(records)
    assert by_purpose["conversation"]["attempts"] == 4
    assert by_purpose["memory_refresh_generate"]["attempts"] == 1
    assert by_purpose["memory_refresh_review"]["attempts"] == 1
    chat_rows = [r for r in records if r["purpose"] == "conversation"]
    memory_rows = [r for r in records if r["purpose"].startswith("memory_refresh")]
    assert {r["request_id"] for r in chat_rows} == {chat_scope.request_id}
    assert all(r["request_id"] is None and r["linked_request_id"] == chat_scope.request_id
               for r in memory_rows)
    assert len({r["job_id"] for r in memory_rows}) == 1
    # The job carries only the id: no chat trace, budget or cancellation state is inherited.
    assert all(seen_trace is None for _scope, seen_trace in memory_model.seen)
    assert trace.budget.sends == 4
    by_request = usage_ledger.summarize(records, by="request")
    assert by_request["request:" + chat_scope.request_id]["measured"]["total_tokens"] == 48
    assert by_request["job:" + memory_rows[0]["job_id"]]["measured"]["total_tokens"] == 24


async def test_ledger_storage_failure_and_full_queue_do_not_change_or_stall_responses(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    broken = usage_ledger._Ledger()
    broken.configure(blocker / "usage", tmp_path)
    monkeypatch.setattr(usage_ledger, "ledger", broken)
    provider = Fake([ok({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})])
    response = await provider.chat_with_retry(messages=MSG)
    assert response.content == ANSWER
    assert broken.flush(2) and broken.write_errors == 1 and len(broken.recent) == 1

    release = threading.Event()
    slow = usage_ledger._Ledger()
    slow.configure(tmp_path / "usage2", tmp_path)
    slow.queue = __import__("queue").Queue(maxsize=1)
    monkeypatch.setattr(slow, "_write", lambda batch: release.wait(5))
    monkeypatch.setattr(usage_ledger, "ledger", slow)
    provider = Fake([ok() for _ in range(5)])
    started = time.monotonic()
    for _ in range(5):
        await provider.chat_with_retry(messages=MSG)
    assert time.monotonic() - started < 1.0
    assert slow.dropped >= 3 and len(slow.recent) == 5
    release.set()
    assert slow.flush(2)


async def test_recorder_exception_keeps_result_and_provider_errors(ledger, monkeypatch):
    def broken(*_a, **_k):
        raise ValueError("ledger bug")
    monkeypatch.setattr(usage_ledger, "build_record", broken)
    error = LLMResponse(content="bad request", finish_reason="error", error_status_code=400)
    provider = Fake([ok(), error])
    assert (await provider.chat_with_retry(messages=MSG)).content == ANSWER
    assert await provider.chat_with_retry(messages=MSG, max_retries=0) is error


async def test_cancellation_propagates_and_is_recorded_as_cancelled(ledger):
    entered = asyncio.Event()

    async def hang():
        entered.set()
        await asyncio.Event().wait()
    provider = Fake([hang])
    task = asyncio.create_task(provider.chat_stream_with_retry(messages=MSG))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (record,) = file_records(ledger)
    assert record["status"] == "cancelled" and record["usage_status"] == "unknown"


async def test_timeout_status_is_distinct(ledger):
    provider = Fake([LLMResponse(content="Error calling LLM: stalled", finish_reason="error",
                                 error_kind="timeout")])
    response = await provider.chat_with_retry(messages=MSG, max_retries=0)
    assert response.error_kind == "timeout"
    (record,) = file_records(ledger)
    assert record["status"] == "timed_out" and record["usage_status"] == "unknown"


async def test_current_path_without_context_selection_is_recorded(ledger, tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import Config

    config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
    assert not config.agents.defaults.context_selection.enabled
    provider = Fake([ok({"prompt_tokens": 70, "completion_tokens": 7, "total_tokens": 77})])
    loop = AgentLoop.from_config(config, provider=provider)
    try:
        result = await loop.process_direct(SECRET)
    finally:
        loop.stop()
    assert result.content == ANSWER
    chat = [r for r in file_records(ledger) if r["purpose"] == "conversation"]
    assert len(chat) == 1 and chat[0]["request_id"] and chat[0]["total_tokens"] == 77
    assert chat[0]["workspace_name"] == tmp_path.name
    assert SECRET not in ledger.path.read_text()


def test_cli_summary_reads_records(ledger, capsys):
    ledger.submit({"v": 1, "logical_call_id": "l", "purpose": "conversation", "status": "succeeded",
                   "usage_status": "measured", "total_tokens": 5, "duration_ms": 3})
    ledger.flush(2)
    assert usage_ledger.main([str(ledger.path), "--by", "purpose"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["groups"]["conversation"]["measured"]["total_tokens"] == 5

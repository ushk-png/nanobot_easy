"""Write-ahead, privacy-safe accounting of every provider attempt."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from nanobot.utils.helpers import estimate_message_tokens


def estimate_component(value: Any) -> int:
    if isinstance(value, list) and all(isinstance(v, dict) and "role" in v for v in value):
        return sum(estimate_message_tokens(v) for v in value)
    return estimate_message_tokens({"role": "user", "content":
                                    value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)})


@dataclass
class ContextTrace:
    request_id: str
    task_id: str | None = None
    stage: str = "design"
    started: float = field(default_factory=time.monotonic)
    calls: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    components: dict[str, int] = field(default_factory=dict)
    store: Any = field(default=None, repr=False)
    pending_milestones: list[dict] = field(default_factory=list, repr=False)

    event_queue: list[tuple[str, int, dict]] = field(default_factory=list, repr=False)
    event_writer: asyncio.Task | None = field(default=None, repr=False)

    def _append_events(self, batch) -> None:
        # This worker only appends immutable events; it never overwrites call status.
        with self.store.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO context_model_events VALUES (?, ?, ?)",
                             [(aid, ordinal, json.dumps(event)) for aid, ordinal, event in batch])

    async def _write_events(self) -> None:
        try:
            while self.event_queue:
                await asyncio.sleep(0.05)
                batch = self.event_queue[:256]
                await asyncio.to_thread(self._append_events, batch)
                del self.event_queue[:len(batch)]
        except Exception:
            # Full lifecycle snapshots still preserve the in-memory records. Keep
            # the queue for diagnosis; do not loop on a failing database.
            logger.warning("Context event journal unavailable; lifecycle snapshot retained")

    async def flush_events(self) -> None:
        if self.event_writer:
            await asyncio.shield(self.event_writer)

    def mark(self, phase: str, *, attempt: str | None = None, **fields: Any) -> None:
        # Only code-owned names/counts/status/options; never response text or errors.
        allowed = {"status", "event_type", "bytes", "count", "timeout_seconds", "delay_ms",
                   "model", "provider", "reasoning_effort", "max_output_tokens", "stream",
                   "tool_choice", "tool_count", "http_status", "schema_hash", "result_hash"}
        allowed.update({"estimated_tokens", "event_sequence", "item_type", "processing_ms"})
        milestone = {"phase": phase, "monotonic_ms": round((time.monotonic() - self.started) * 1000, 3),
                     **{k: v for k, v in fields.items() if k in allowed}}
        call = next((c for c in self.calls if c.get("attempt_id") == attempt), None)
        if call is None:
            self.pending_milestones.append(milestone)
            return
        milestone["attempt_id"] = attempt
        call.setdefault("milestones", []).append(milestone)
        if phase.startswith("stream_"):
            if self.store:
                self.event_queue.append((attempt, len(call["milestones"]) - 1, milestone))
                if self.event_writer is None or self.event_writer.done():
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        # Synchronous callers retain events in their next lifecycle snapshot.
                        return
                    self.event_writer = loop.create_task(self._write_events())
            return
        self.persist()

    def bind(self, store: Any) -> None:
        self.store = store
        self.task_id = self.task_id or "pending:" + uuid.uuid4().hex
        with store.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS context_model_attempts (
                attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, call_sequence INTEGER NOT NULL,
                task_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(request_id, call_sequence))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS context_model_events (
                attempt_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(attempt_id, ordinal))""")

    def count(self, name: str, reason: str | None = None) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1
        if reason and reason not in self.reasons:
            self.reasons.append(reason)

    def begin(self, kwargs: dict[str, Any]) -> str:
        """Commit the attempt before dispatch; no prompt, credentials or output text is stored."""
        self.task_id = self.task_id or "pending:" + uuid.uuid4().hex
        sequence = len(self.calls) + 1
        previous = self.calls[-1] if self.calls else None
        call = {"attempt_id": uuid.uuid4().hex, "request_id": self.request_id, "task_id": self.task_id,
                "stage": self.stage, "call_sequence": sequence, "status": "in_flight",
                "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
                "retry_of": previous.get("attempt_id") if previous and previous.get("status") in
                    {"failed", "cancelled", "timed_out", "result_unknown"} else None,
                "estimated_input_tokens": estimate_component(kwargs.get("messages", []))
                    + estimate_component(kwargs.get("tools") or []),
                "estimated_output_tokens": None, "provider_usage": None, "elapsed_ms": None, "error": False,
                "component_estimated_tokens": dict(self.components),
                "milestones": self.pending_milestones}
        self.pending_milestones = []
        for milestone in call["milestones"]:
            milestone["attempt_id"] = call["attempt_id"]
        if self.store:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                latest = conn.execute("SELECT MAX(call_sequence) FROM context_model_attempts WHERE request_id=?",
                                      (self.request_id,)).fetchone()[0]
                call["call_sequence"] = max(sequence, int(latest or 0) + 1)
                conn.execute("INSERT INTO context_model_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (call["attempt_id"], self.request_id, call["call_sequence"], self.task_id,
                              self.stage, call["status"], json.dumps(call)))
        self.calls.append(call)
        self.persist()
        return call["attempt_id"]

    def end(self, attempt: str, response: Any, elapsed: float, status: str | None = None) -> None:
        call = next(c for c in self.calls if c.get("attempt_id") == attempt)
        # An end/update rewrites the same attempt; it never creates a duplicate call.
        call["status"] = status or ("failed" if response.finish_reason == "error" else "succeeded")
        call["finished_at"] = datetime.now(timezone.utc).isoformat()
        call["elapsed_ms"] = int(elapsed * 1000)
        call["error"] = call["status"] != "succeeded"
        if response is not None:
            call["finish_reason"] = response.finish_reason
            call["response_shape"] = {"content_bytes": len((response.content or "").encode()),
                                      "tool_call_count": len(response.tool_calls)}
            usage = {k: v for k, v in (response.usage or {}).items()
                     if isinstance(v, int) and not isinstance(v, bool)}
            call["provider_usage"] = usage or None
            call["estimated_output_tokens"] = (estimate_component(response.content or "")
                + estimate_component([getattr(t, "arguments", {}) for t in response.tool_calls])
                + estimate_component(getattr(response, "reasoning_content", None) or ""))
        self.persist()

    def mark_timeout(self) -> None:
        if self.calls and self.calls[-1].get("status") == "cancelled":
            self.calls[-1]["status"] = "timed_out"
            self.persist()

    def record(self, kwargs: dict[str, Any], response: Any, elapsed: float) -> None:
        self.end(self.begin(kwargs), response, elapsed)

    def recover_abandoned(self) -> None:
        """Explicit restart recovery for this request, after the old worker is known stopped."""
        if not self.store:
            return
        with self.store.connect() as conn:
            rows = conn.execute("SELECT payload FROM context_model_attempts WHERE request_id=? ORDER BY call_sequence",
                                (self.request_id,)).fetchall()
        self.calls = [json.loads(row[0]) for row in rows]
        for call in self.calls:
            with self.store.connect() as conn:
                events = conn.execute("SELECT ordinal, payload FROM context_model_events "
                                      "WHERE attempt_id=? ORDER BY ordinal",
                                      (call["attempt_id"],)).fetchall()
            merged = dict(enumerate(call.get("milestones", [])))
            merged.update({ordinal: json.loads(payload) for ordinal, payload in events})
            call["milestones"] = [merged[i] for i in sorted(merged)]
            if call["status"] == "in_flight":
                call["status"] = "result_unknown"
        self.persist()

    def report(self) -> dict[str, Any]:
        missing = sum(not c.get("provider_usage") for c in self.calls)
        known = sum((c.get("provider_usage") or {}).get("total_tokens", 0)
                    or ((c.get("provider_usage") or {}).get("prompt_tokens", 0)
                        + (c.get("provider_usage") or {}).get("completion_tokens", 0)) for c in self.calls)
        return {"request_id": self.request_id, "task_id": self.task_id,
                "calls": self.calls, "counters": self.counters, "reasons": self.reasons,
                "component_estimated_tokens": self.components,
                "elapsed_ms": int((time.monotonic() - self.started) * 1000),
                "estimated_total_tokens": sum((c.get("estimated_input_tokens") or 0)
                    + (c.get("estimated_output_tokens") or 0) for c in self.calls),
                "provider_usage_calls": len(self.calls) - missing, "provider_usage_missing_calls": missing,
                "provider_usage_status": "unavailable" if missing == len(self.calls) else "partial" if missing else "complete",
                "provider_total_tokens": None if missing else known, "known_provider_total_tokens": known}

    def persist(self, store: Any = None) -> None:
        store = store or self.store
        if store is None:
            return
        with store.connect() as conn:
            # Legacy callers can persist aggregate-only traces before bind().
            conn.execute("INSERT OR REPLACE INTO context_selection_runs VALUES (?, ?, ?)",
                         (self.request_id, self.task_id, json.dumps(self.report())))
            if self.store:
                for call in self.calls:
                    call["task_id"] = self.task_id
                    if call.get("attempt_id"):
                        conn.execute("UPDATE context_model_attempts SET task_id=?, status=?, payload=? WHERE attempt_id=?",
                                     (self.task_id, call["status"], json.dumps(call), call["attempt_id"]))


active_context_trace: ContextVar[ContextTrace | None] = ContextVar("context_trace", default=None)

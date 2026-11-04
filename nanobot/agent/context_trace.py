"""Write-ahead, privacy-safe accounting of every provider attempt."""
from __future__ import annotations

import asyncio
import json
import threading
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


# Serialises this process's writes to the context tables. The event journal is
# written by a background thread while begin/end/persist write on the caller's
# thread; one lock prevents "database is locked" contention between the two.
# Only short single-row (or one-batch) transactions are done while it is held.
_DB_LOCK = threading.Lock()
_EVENT_BATCH_SIZE = 512
_EVENT_COALESCE_SECONDS = 0.05


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
    # Bumped on every caller-thread write of an attempt row. The background
    # writer only refreshes a finished attempt's row when no newer write raced it.
    row_revisions: dict[str, int] = field(default_factory=dict, repr=False)
    flush_requested: bool = field(default=False, repr=False)
    background: list = field(default_factory=list, repr=False)   # text-accepted streams draining for usage
    budget: Any = field(default=None, repr=False)   # t54: whole-request TaskBudget shared by every send

    # ---- background event journal (append-only deltas, off the event loop) ----

    def _append_events(self, batch) -> None:
        # This worker only appends immutable events; it never overwrites call status.
        with _DB_LOCK, self.store.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO context_model_events VALUES (?, ?, ?)",
                             [(aid, ordinal, json.dumps(event)) for aid, ordinal, event in batch])

    def _refresh_finished_rows(self, snapshots) -> None:
        """Fold milestones that arrived after a call ended (e.g. cleanup) into its row.

        Runs in a worker thread. A snapshot is written only if no caller-thread
        write happened after it was taken, so a final status is never regressed.
        """
        with _DB_LOCK, self.store.connect() as conn:
            for aid, revision, status, task_id, call in snapshots:
                if self.row_revisions.get(aid, 0) == revision:
                    conn.execute("UPDATE context_model_attempts SET task_id=?, status=?, payload=? "
                                 "WHERE attempt_id=?", (task_id, status, json.dumps(call), aid))

    def _finished_snapshots(self, batch) -> list[tuple]:
        attempts = {aid for aid, _, _ in batch}
        return [(call["attempt_id"], self.row_revisions.get(call["attempt_id"], 0), call["status"],
                 self.task_id, {**call, "task_id": self.task_id,
                                "milestones": list(call.get("milestones", []))})
                for call in self.calls
                if call.get("attempt_id") in attempts and call.get("finished_at")]

    async def _write_events(self) -> None:
        try:
            if not self.flush_requested:
                # Coalesce a burst of stream events into one transaction.
                await asyncio.sleep(_EVENT_COALESCE_SECONDS)
            while self.event_queue:
                batch = self.event_queue[:_EVENT_BATCH_SIZE]
                snapshots = self._finished_snapshots(batch)
                await asyncio.to_thread(self._append_events, batch)
                del self.event_queue[:len(batch)]
                if snapshots:
                    await asyncio.to_thread(self._refresh_finished_rows, snapshots)
        except Exception:
            # Lifecycle snapshots (begin/end/persist) still carry the in-memory
            # records. Keep the queue for diagnosis; do not loop on a failing database.
            logger.warning("Context event journal unavailable; lifecycle snapshot retained")
        finally:
            self.flush_requested = False

    def _kick_writer(self) -> None:
        if not self.store or not self.event_queue:
            return
        if self.event_writer is not None and not self.event_writer.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous callers retain events in their next lifecycle snapshot.
            return
        self.event_writer = loop.create_task(self._write_events(), name="context-trace-journal")

    async def flush_events(self, timeout: float | None = None) -> None:
        """Wait until queued milestones are journaled (bounded when timeout is given)."""
        if not self.store:
            return
        if self.event_queue:
            self.flush_requested = True
            self._kick_writer()
        writer = self.event_writer
        if writer is None or writer.done():
            return
        if timeout is None:
            await asyncio.shield(writer)
            return
        try:
            await asyncio.wait_for(asyncio.shield(writer), timeout)
        except TimeoutError:
            logger.warning("Context event journal flush still running; continuing without waiting")

    # ---- milestones: in memory + queued delta, never a synchronous write ----

    def mark(self, phase: str, *, attempt: str | None = None, **fields: Any) -> None:
        # Only code-owned names/counts/status/options; never response text or errors.
        allowed = {"status", "event_type", "bytes", "count", "timeout_seconds", "delay_ms",
                   "model", "provider", "reasoning_effort", "max_output_tokens", "stream",
                   "tool_choice", "tool_count", "http_status", "schema_hash", "result_hash"}
        allowed.update({"estimated_tokens", "event_sequence", "item_type", "processing_ms"})
        # stream_log: the bounded, content-free StreamEventLog summary (one per stream).
        allowed.add("stream_log")
        milestone = {"phase": phase, "monotonic_ms": round((time.monotonic() - self.started) * 1000, 3),
                     **{k: v for k, v in fields.items() if k in allowed}}
        call = next((c for c in self.calls if c.get("attempt_id") == attempt), None)
        if call is None:
            # Attached to (and written with) the next attempt row at begin().
            self.pending_milestones.append(milestone)
            return
        milestone["attempt_id"] = attempt
        milestones = call.setdefault("milestones", [])
        milestones.append(milestone)
        if self.store:
            # Only the new milestone is queued; the writer appends it off-loop.
            self.event_queue.append((attempt, len(milestones) - 1, milestone))
            self._kick_writer()

    def bind(self, store: Any) -> None:
        self.store = store
        self.task_id = self.task_id or "pending:" + uuid.uuid4().hex
        with _DB_LOCK, store.connect() as conn:
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

    # ---- call lifecycle: written immediately, one row each ----

    def _write_call(self, call: dict[str, Any]) -> None:
        if not self.store or not call.get("attempt_id"):
            return
        call["task_id"] = self.task_id
        with _DB_LOCK:
            self.row_revisions[call["attempt_id"]] = self.row_revisions.get(call["attempt_id"], 0) + 1
            with self.store.connect() as conn:
                conn.execute("UPDATE context_model_attempts SET task_id=?, status=?, payload=? WHERE attempt_id=?",
                             (self.task_id, call["status"], json.dumps(call), call["attempt_id"]))

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
            # Write-ahead: only this attempt's row, before the request is sent.
            with _DB_LOCK:
                with self.store.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    latest = conn.execute("SELECT MAX(call_sequence) FROM context_model_attempts WHERE request_id=?",
                                          (self.request_id,)).fetchone()[0]
                    call["call_sequence"] = max(sequence, int(latest or 0) + 1)
                    conn.execute("INSERT INTO context_model_attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (call["attempt_id"], self.request_id, call["call_sequence"], self.task_id,
                                  self.stage, call["status"], json.dumps(call)))
                self.row_revisions[call["attempt_id"]] = 1
        self.calls.append(call)
        self._kick_writer()
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
        # Final state of this attempt: one row, written now (full milestones once).
        self._write_call(call)
        self.flush_requested = True
        self._kick_writer()

    async def settle_background(self, timeout: float) -> None:
        import asyncio
        pending = [t for t in self.background if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def mark_timeout(self) -> None:
        if self.calls and self.calls[-1].get("status") == "cancelled":
            self.calls[-1]["status"] = "timed_out"
            self._write_call(self.calls[-1])

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
        """Lifecycle snapshot: the run row plus each attempt row, written once per call site.

        Milestones are never persisted from mark(); callers persist at the end of
        a design/execution stage (and begin/end write their own attempt row), so
        the full cumulative JSON is serialised a bounded number of times per run.
        """
        store = store or self.store
        if store is None:
            return
        with _DB_LOCK:
            with store.connect() as conn:
                # Legacy callers can persist aggregate-only traces before bind().
                conn.execute("INSERT OR REPLACE INTO context_selection_runs VALUES (?, ?, ?)",
                             (self.request_id, self.task_id, json.dumps(self.report())))
                if self.store:
                    for call in self.calls:
                        call["task_id"] = self.task_id
                        if call.get("attempt_id"):
                            self.row_revisions[call["attempt_id"]] = self.row_revisions.get(call["attempt_id"], 0) + 1
                            conn.execute("UPDATE context_model_attempts SET task_id=?, status=?, payload=? WHERE attempt_id=?",
                                         (self.task_id, call["status"], json.dumps(call), call["attempt_id"]))
        if self.event_queue:
            self.flush_requested = True
            self._kick_writer()

active_context_trace: ContextVar[ContextTrace | None] = ContextVar("context_trace", default=None)

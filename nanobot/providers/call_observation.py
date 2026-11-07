"""Content-free milestones scoped to one actual provider attempt."""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from loguru import logger

observer: ContextVar[Callable[..., None] | None] = ContextVar("provider_call_observer", default=None)
# (monotonic, wall epoch seconds) when the current provider attempt started.
call_clock: ContextVar[tuple[float, float] | None] = ContextVar("provider_call_clock", default=None)
_cleanup_tasks: set[asyncio.Task] = set()
CLEANUP_TIMEOUT_SECONDS = 5


def observe(phase: str, **fields: Any) -> None:
    callback = observer.get()
    if callback:
        try:
            callback(phase, **fields)
        except Exception:
            # A failed secondary milestone write must not corrupt model output.
            # The mandatory write-ahead attempt itself is still fail-closed.
            logger.warning("Provider milestone persistence unavailable")


async def transport_trace(name: str, info: dict) -> None:
    # HTTPcore's HTTPX trace extension. Never retain info: it can contain
    # headers, URLs, credentials or socket objects. Reused connections may
    # legitimately omit connect/TLS events.
    for suffix, phase in {
        "connect_tcp.complete": "connection_established",
        "start_tls.complete": "tls_established",
        "send_request_headers.complete": "request_headers_sent",
    }.items():
        if name.endswith(suffix):
            observe(phase)
            break


def schedule_cleanup(*closers: Callable) -> asyncio.Task:
    """Close owned resources independently of result delivery and its deadline.

    Keep a strong task reference; bound each close separately so one broken
    resource cannot prevent attempting the next. Never convert a completed
    model result into an error due to disposal failure. Process exit can still
    interrupt disposal; the journal then retains cleanup_started without end.
    """
    async def close():
        observe("cleanup_started")
        outcomes = []
        for index, closer in enumerate(closers):
            observe("resource_cleanup_started", count=index)
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await closer()
                outcomes.append("closed")
            except TimeoutError:
                outcomes.append("timed_out")
            except asyncio.CancelledError:
                observe("cleanup_finished", status="cancelled")
                raise
            except Exception:
                outcomes.append("error")
            observe("resource_cleanup_finished", count=index, status=outcomes[-1])
        observe("cleanup_finished", status="closed" if all(o == "closed" for o in outcomes)
                else "timed_out" if "timed_out" in outcomes else "error")

    task = asyncio.create_task(close(), name="provider-response-cleanup")
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)
    return task


@contextmanager
def observe_stream_event(event: dict, sequence: int):
    """Pair receipt and processing, including callback cancellation, without payloads."""
    known_events = {
        "response.created", "response.in_progress", "response.completed",
        "response.failed", "response.incomplete", "response.cancelled", "error",
        "response.output_item.added", "response.output_item.done",
        "response.content_part.added", "response.content_part.done",
        "response.output_text.delta", "response.output_text.done",
        "response.function_call_arguments.delta", "response.function_call_arguments.done",
        "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
    }
    item = event.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    fields = {
        "event_sequence": sequence,
        "event_type": event.get("type") if event.get("type") in known_events else "unknown",
        "item_type": item_type if item_type in {"reasoning", "message", "function_call"}
                     else "other" if item_type else "none",
    }
    started = time.monotonic()
    observe("stream_event_received", **fields)
    status = "processed"
    try:
        yield
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except BaseException:
        status = "error"
        raise
    finally:
        observe("stream_event_processed", **fields, status=status,
                processing_ms=round((time.monotonic() - started) * 1000, 3))


# ---- lightweight stream event log: one summary milestone per stream ----

_KNOWN_EVENTS = frozenset({
    "response.created", "response.in_progress", "response.completed",
    "response.failed", "response.incomplete", "response.cancelled", "error",
    "response.output_item.added", "response.output_item.done",
    "response.content_part.added", "response.content_part.done",
    "response.output_text.delta", "response.output_text.done",
    "response.function_call_arguments.delta", "response.function_call_arguments.done",
    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
    "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
})
_ITEM_TYPES = frozenset({"reasoning", "message", "function_call"})
_DELTA_ITEM_TYPE = {
    "response.output_text.delta": "message",
    "response.function_call_arguments.delta": "function_call",
    "response.reasoning_summary_text.delta": "reasoning",
}
STREAM_LOG_MAX_EVENTS = 256      # ordered, non-coalesced entries per stream
STREAM_LOG_MAX_DELTA_KEYS = 64   # coalesced delta series per stream
STREAM_LOG_MAX_FUNCTION_CALLS = 32


class StreamEventLog:
    """In-memory, content-free record of one provider stream.

    The hot path stores only small lists of short strings/ints/floats: no
    payload, no ids, no serialisation and no milestone per event. Delta events
    are coalesced per (event type, item): the first one is kept in the ordered
    list, later ones only bump a counter and the last-seen time. Everything is
    emitted once, as the ``stream_event_log`` milestone, when the stream ends
    for any reason (terminal event, error, EOF, cancellation/deadline), and goes
    through the trace's existing asynchronous event journal.
    """

    __slots__ = ("base", "wall", "opened_ms", "seq", "events", "dropped", "deltas", "calls",
                 "aliases", "added", "done", "last", "emitted")

    def __init__(self) -> None:
        now = time.monotonic()
        clock = call_clock.get()
        self.base, self.wall = clock if clock else (now, time.time())
        self.opened_ms = round((now - self.base) * 1000, 1)
        self.seq = 0
        self.events: list[list] = []
        self.dropped = 0
        self.deltas: dict[tuple, list] = {}
        self.calls: list[list] = []
        self.aliases: dict[Any, int] = {}
        self.added: dict[str, int] = {}
        self.done: dict[str, int] = {}
        self.last: list | None = None
        self.emitted = False

    def _call(self, event: dict, item: dict | None) -> list | None:
        keys = [k for k in ((item or {}).get("call_id"), (item or {}).get("id"),
                            event.get("call_id"), event.get("item_id"))
                if isinstance(k, (str, int)) and k != ""]
        for key in keys:
            index = self.aliases.get(key)
            if index is not None:
                return self.calls[index]
        if len(self.calls) >= STREAM_LOG_MAX_FUNCTION_CALLS:
            self.dropped += 1
            return None
        # [added_ms, args_first_delta_ms, args_done_ms, item_done_ms, args_delta_count]
        entry = [None, None, None, None, 0]
        self.calls.append(entry)
        for key in keys:
            self.aliases[key] = len(self.calls) - 1
        return entry

    def record(self, event: dict) -> None:
        ms = round((time.monotonic() - self.base) * 1000, 1)
        self.seq += 1
        raw_type = event.get("type")
        event_type = raw_type if raw_type in _KNOWN_EVENTS else "unknown"
        item = event.get("item")
        item = item if isinstance(item, dict) else None
        raw_item = item.get("type") if item else None
        item_type = (raw_item if raw_item in _ITEM_TYPES else "other" if raw_item
                     else _DELTA_ITEM_TYPE.get(event_type, "none"))
        short = event_type[9:] if event_type.startswith("response.") else event_type
        self.last = [short, item_type, ms]
        if event_type in _DELTA_ITEM_TYPE:
            key = event.get("item_id") or event.get("call_id")
            key = (short, key if isinstance(key, (str, int)) else None, event.get("output_index")
                   if isinstance(event.get("output_index"), int) else None)
            series = self.deltas.get(key)
            if series is not None:
                series[3] = ms
                series[4] += 1
            elif len(self.deltas) < STREAM_LOG_MAX_DELTA_KEYS:
                # [event_type, item_type, first_ms, last_ms, count]
                self.deltas[key] = [short, item_type, ms, ms, 1]
                self._append(short, item_type, ms)
            else:
                self.dropped += 1
            if event_type == "response.function_call_arguments.delta":
                call = self._call(event, None)
                if call is not None:
                    if call[1] is None:
                        call[1] = ms
                    call[4] += 1
            return
        self._append(short, item_type, ms)
        if event_type == "response.output_item.added":
            self.added[item_type] = self.added.get(item_type, 0) + 1
            if item_type == "function_call":
                call = self._call(event, item)
                if call is not None and call[0] is None:
                    call[0] = ms
        elif event_type == "response.output_item.done":
            self.done[item_type] = self.done.get(item_type, 0) + 1
            if item_type == "function_call":
                call = self._call(event, item)
                if call is not None:
                    call[3] = ms
                    if call[2] is None:
                        call[2] = ms
        elif event_type == "response.function_call_arguments.done":
            call = self._call(event, None)
            if call is not None:
                call[2] = ms

    def _append(self, short: str, item_type: str, ms: float) -> None:
        if len(self.events) < STREAM_LOG_MAX_EVENTS:
            # [event_sequence, event_type, item_type, offset_ms]
            self.events.append([self.seq, short, item_type, ms])
        else:
            self.dropped += 1

    def summary(self) -> dict[str, Any]:
        now_ms = round((time.monotonic() - self.base) * 1000, 1)
        return {
            "v": 1,
            "call_started_wall": round(self.wall, 3),
            "stream_opened_ms": self.opened_ms,
            "events_seen": self.seq,
            "events": self.events,
            "deltas": list(self.deltas.values()),
            "function_calls": self.calls,
            "items_added": self.added,
            "items_done": self.done,
            "function_args_started": sum(c[1] is not None for c in self.calls),
            "function_args_completed": sum(c[2] is not None for c in self.calls),
            "last_event": self.last,
            "since_last_event_ms": round(now_ms - self.last[2], 1) if self.last else None,
            "elapsed_ms": now_ms,
            "dropped": self.dropped,
        }

    def emit(self, status: str) -> None:
        """Hand the summary to the attempt's milestone sink, once."""
        if self.emitted:
            return
        self.emitted = True
        observe("stream_event_log", status=status, count=self.seq, stream_log=self.summary())


def stream_event_log() -> StreamEventLog | None:
    """A recorder only when a trace observes this attempt; otherwise zero overhead."""
    return StreamEventLog() if observer.get() is not None else None

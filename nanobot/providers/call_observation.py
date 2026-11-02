"""Content-free milestones scoped to one actual provider attempt."""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from loguru import logger

observer: ContextVar[Callable[..., None] | None] = ContextVar("provider_call_observer", default=None)
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

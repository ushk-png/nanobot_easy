"""Content-free, bounded ledger of actual provider attempts (measurement only).

One record per leaf ``LLMProvider._safe_chat`` / ``_safe_chat_stream`` attempt. Routing wrappers
(``journals_provider_attempts``) are skipped so a fallback attempt is never counted twice, and the
record is taken from the attempt's single returned response, so streamed chunks and the final usage
cannot be added together. Records hold only ids, code-owned labels, timings and token counts: never
prompts, responses, tool results, memory text, error text, session keys or credentials.

Usage semantics (normalised by the providers to ``prompt_tokens`` / ``completion_tokens`` /
``total_tokens``): cached/cache-write tokens are a breakdown of the input and reasoning tokens a
breakdown of the output, so they are reported but never added to the total. A missing or all-zero
usage is ``unknown`` (never 0); a successful attempt without usage gets a separate local estimate.

Recording never raises into the caller and never blocks: the file sink is a bounded queue drained by
one daemon thread (one line per attempt, size-rotated). Nothing here is sent to the model or off-box.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from loguru import logger

SCHEMA_VERSION = 1
DISABLE_ENV = "NANOBOT_USAGE_LEDGER_DISABLE"
MAX_QUEUE = 1000          # pending file writes; beyond this records are dropped and counted
MAX_RECENT = 2000         # in-process ring of recent records
MAX_FILE_BYTES = 5 * 1024 * 1024
FILE_NAME = "model-attempts.jsonl"
_LABEL_MAX = 48


@dataclass(frozen=True)
class UsageScope:
    """Who an attempt is for. Ids are opaque; no session key or chat id is kept."""

    purpose: str = "unattributed"
    workspace_id: str | None = None
    workspace_name: str | None = None
    request_id: str | None = None          # a logical user/automation request (one turn)
    job_id: str | None = None              # a separate background job
    linked_request_id: str | None = None   # the request that started the job, when known


_scope: ContextVar[UsageScope | None] = ContextVar("nanobot_usage_scope", default=None)
# [logical_call_id, attempts_so_far] for one chat_with_retry / chat_stream_with_retry call.
_logical: ContextVar[list | None] = ContextVar("nanobot_usage_logical_call", default=None)


def opaque_id(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def _workspace_ref(workspace: Any) -> tuple[str | None, str | None]:
    if workspace is None:
        return None, None
    try:
        path = Path(workspace).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, None
    return opaque_id(path), path.name[:_LABEL_MAX] or None


def purpose_for_session_key(session_key: str | None) -> str:
    key = session_key or ""
    if key.startswith("dream:"):
        return "dream"
    if key == "heartbeat" or key.startswith("cron:"):
        return "cron"
    if key.startswith("api:"):
        return "api"
    if key.startswith("system:"):
        return "system"
    return "conversation"


def current_scope() -> UsageScope | None:
    return _scope.get()


def request_scope(purpose: str, *, workspace: Any, request_key: Any) -> UsageScope:
    ws_id, ws_name = _workspace_ref(workspace)
    return UsageScope(purpose=purpose, workspace_id=ws_id, workspace_name=ws_name,
                      request_id=opaque_id(request_key))


def background_job(purpose: str, *, parent: UsageScope | None = None,
                   workspace: Any = None) -> UsageScope:
    """A separate job. It is linked only to the request it was started from (if any); none of the
    request's budget, cancellation or permission state is carried, only this id."""
    ws_id, ws_name = _workspace_ref(workspace) if workspace is not None else (
        (parent.workspace_id, parent.workspace_name) if parent else (None, None))
    linked = (parent.request_id or parent.linked_request_id) if parent else None
    return UsageScope(purpose=purpose, workspace_id=ws_id, workspace_name=ws_name,
                      job_id=uuid.uuid4().hex, linked_request_id=linked)


def enter(scope: UsageScope | None):
    return _scope.set(scope)


def leave(token) -> None:
    _scope.reset(token)


@contextmanager
def bound(scope: UsageScope | None, purpose: str | None = None) -> Iterator[None]:
    if purpose:
        scope = replace(scope, purpose=purpose) if scope is not None else UsageScope(purpose=purpose)
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def logical_call() -> Iterator[None]:
    """One logical model request; nested provider layers (fallback) reuse the outer id."""
    if _logical.get() is not None:
        yield
        return
    token = _logical.set([uuid.uuid4().hex, 0])
    try:
        yield
    finally:
        _logical.reset(token)


# ---- normalisation ---------------------------------------------------------------------------

def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(raw: Any) -> dict[str, Any] | None:
    """Provider-reported usage, or None when unknown. Breakdown fields are never re-added."""
    if not isinstance(raw, dict):
        return None
    inp, out, total = (_count(raw.get(k)) for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
    if (inp or 0) + (out or 0) + (total or 0) <= 0:
        return None
    # Some adapters use zero as the default for an absent total.
    total = total if total else None
    cache_read = _count(raw.get("cached_tokens"))
    if cache_read is None:
        cache_read = _count(raw.get("cache_read_input_tokens"))
    total_source = "provider" if total is not None else "unknown"
    if total is None and inp is not None and out is not None:
        total, total_source = inp + out, "input_plus_output"
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
        "total_source": total_source,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": _count(raw.get("cache_creation_input_tokens")),
        "reasoning_tokens": _count(raw.get("reasoning_tokens")),
    }


def _estimate(kwargs: dict[str, Any], response: Any) -> dict[str, int] | None:
    from nanobot.agent.context_trace import estimate_component

    inp = estimate_component(kwargs.get("messages") or []) + estimate_component(kwargs.get("tools") or [])
    out = (estimate_component(getattr(response, "content", None) or "")
           + estimate_component([getattr(t, "arguments", {}) for t in getattr(response, "tool_calls", [])]))
    if inp + out <= 0:
        return None
    return {"estimated_input_tokens": inp, "estimated_output_tokens": out,
            "estimated_total_tokens": inp + out}


def _label(value: Any) -> str | None:
    return value[:_LABEL_MAX] if isinstance(value, str) and value else None


def _status(response: Any, outcome: str | None) -> str:
    if outcome == "cancelled":
        return "cancelled"
    if response is None:
        return "failed"
    if getattr(response, "finish_reason", None) == "error":
        return "timed_out" if (getattr(response, "error_kind", None) or "").lower() == "timeout" else "failed"
    return "succeeded"


def build_record(provider: Any, kwargs: dict[str, Any], *, stream: bool, elapsed_s: float,
                 response: Any, outcome: str | None = None) -> dict[str, Any]:
    scope = _scope.get() or UsageScope()
    logical = _logical.get()
    if logical is None:
        logical_id, index = uuid.uuid4().hex, 1
    else:
        logical[1] += 1
        logical_id, index = logical
    model = kwargs.get("model")
    if not model:
        try:
            model = provider.get_default_model()
        except Exception:
            model = None
    status = _status(response, outcome)
    record: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "attempt_id": uuid.uuid4().hex,
        "logical_call_id": logical_id,
        "attempt_index": index,
        "workspace_id": scope.workspace_id,
        "workspace_name": scope.workspace_name,
        "request_id": scope.request_id,
        "job_id": scope.job_id,
        "linked_request_id": scope.linked_request_id,
        "purpose": scope.purpose,
        "provider": type(provider).__name__,
        "model": _label(model) if isinstance(model, str) else None,
        "stream": bool(stream),
        "started_at": datetime.fromtimestamp(time.time() - max(0.0, elapsed_s), timezone.utc)
                      .isoformat(timespec="milliseconds"),
        "duration_ms": int(max(0.0, elapsed_s) * 1000),
        "status": status,
        "finish_reason": _label(getattr(response, "finish_reason", None)) if response else outcome,
        "error_kind": _label(getattr(response, "error_kind", None)) if response else None,
    }
    usage = normalize_usage(getattr(response, "usage", None)) if response is not None else None
    if usage:
        record["usage_status"] = "measured"
        record.update(usage)
    else:
        estimate = _estimate(kwargs, response) if status == "succeeded" else None
        record["usage_status"] = "estimated" if estimate else "unknown"
        record.update(estimate or {})
    return record


# ---- sink ------------------------------------------------------------------------------------

class _Ledger:
    def __init__(self) -> None:
        self.recent: deque[dict] = deque(maxlen=MAX_RECENT)
        self.path: Path | None = None
        self.queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
        self.thread: threading.Thread | None = None
        self.dropped = 0
        self.write_errors = 0
        self._lock = threading.Lock()

    def configure(self, directory: Path | None, workspace: Any = None) -> Path | None:
        if directory is None or os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes"}:
            self.path = None
            return None
        ws_id, ws_name = _workspace_ref(workspace)
        folder = Path(directory) / (f"{ws_name or 'workspace'}-{ws_id[:8]}" if ws_id else "default")
        self.path = folder / FILE_NAME
        return self.path

    def submit(self, record: dict) -> None:
        self.recent.append(record)
        if self.path is None:
            return
        try:
            self.queue.put_nowait((self.path, record))
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning("Usage ledger queue full; {} records dropped, usage totals incomplete",
                               self.dropped)
            return
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        with self._lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="usage-ledger", daemon=True)
                self.thread.start()

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            batch = [item]
            while len(batch) < 256:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write(batch)
            except Exception as exc:
                self.write_errors += len(batch)
                if self.write_errors == len(batch) or self.write_errors % 100 < len(batch):
                    logger.warning("Usage ledger write unavailable ({}); responses unaffected",
                                   type(exc).__name__)
            finally:
                for _ in batch:
                    self.queue.task_done()

    @staticmethod
    def _write(batch: list[tuple[Path, dict]]) -> None:
        by_path: dict[Path, list[str]] = {}
        for path, record in batch:
            by_path.setdefault(path, []).append(json.dumps(record, separators=(",", ":")) + "\n")
        for path, lines in by_path.items():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                if path.stat().st_size >= MAX_FILE_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write("".join(lines))

    def flush(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True


ledger = _Ledger()
atexit.register(lambda: ledger.flush(1.0))


def configure(directory: Path | None, workspace: Any = None) -> Path | None:
    """Enable the file sink (gateway startup). Unconfigured processes keep only the memory ring."""
    return ledger.configure(directory, workspace)


def configure_gateway(workspace: Any) -> Path | None:
    """A telemetry directory failure must not prevent the gateway from starting."""
    try:
        from nanobot.config.paths import get_runtime_subdir

        path = configure(get_runtime_subdir("usage"), workspace)
        if path is not None:
            logger.info("Usage ledger configured; bounded per-attempt model usage recording enabled")
        return path
    except Exception as exc:
        ledger.path = None
        logger.warning("Usage ledger initialization unavailable ({}); gateway continues",
                       type(exc).__name__)
        return None


def record_attempt(provider: Any, kwargs: dict[str, Any], *, stream: bool, elapsed_s: float,
                   response: Any, outcome: str | None = None) -> None:
    """Never raises: a measurement failure must not change the model result or its errors."""
    if getattr(provider, "journals_provider_attempts", False):
        return  # routing wrapper; each leaf attempt records itself
    try:
        ledger.submit(build_record(provider, kwargs, stream=stream, elapsed_s=elapsed_s,
                                   response=response, outcome=outcome))
    except Exception:
        logger.debug("Usage ledger record skipped")


# ---- reading / aggregation -------------------------------------------------------------------

def read_records(path: Path) -> list[dict]:
    records: list[dict] = []
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            with open(candidate, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("v") == SCHEMA_VERSION:
                        records.append(item)
        except FileNotFoundError:
            continue
    return records


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
                 "cache_write_tokens", "reasoning_tokens")


def _group_key(record: dict, by: str) -> str:
    if by == "request":
        if record.get("request_id"):
            return "request:" + record["request_id"]
        return "job:" + record["job_id"] if record.get("job_id") else "unscoped"
    return str(record.get(by) or "unknown")


def summarize(records: Iterable[dict], by: str = "purpose") -> dict[str, dict]:
    """Per-group totals. Measured, estimated and unknown are kept apart and never mixed."""
    groups: dict[str, dict] = {}
    logical: dict[str, set] = {}
    reported: dict[str, dict[str, int]] = {}
    for record in records:
        key = _group_key(record, by)
        row = groups.setdefault(key, {
            "logical_requests": 0, "attempts": 0, "status": {}, "duration_ms": 0,
            "measured_attempts": 0, "measured": {f: 0 for f in _TOKEN_FIELDS},
            "estimated_attempts": 0, "estimated_total_tokens": 0, "unknown_usage_attempts": 0,
        })
        logical.setdefault(key, set()).add(record.get("logical_call_id"))
        row["attempts"] += 1
        row["status"][record.get("status")] = row["status"].get(record.get("status"), 0) + 1
        row["duration_ms"] += int(record.get("duration_ms") or 0)
        usage_status = record.get("usage_status")
        if usage_status == "measured":
            row["measured_attempts"] += 1
            for field in _TOKEN_FIELDS:
                if isinstance(record.get(field), int):
                    row["measured"][field] += record[field]
                    reported.setdefault(key, {}).setdefault(field, 0)
                    reported[key][field] += 1
        elif usage_status == "estimated":
            row["estimated_attempts"] += 1
            row["estimated_total_tokens"] += int(record.get("estimated_total_tokens") or 0)
        else:
            row["unknown_usage_attempts"] += 1
    for key, row in groups.items():
        row["logical_requests"] = len(logical[key])
        # A field no measured attempt reported is unknown (null), not 0.
        row["measured"] = {f: (v if reported.get(key, {}).get(f) else None)
                           for f, v in row["measured"].items()}
        row["measured_field_reported_attempts"] = dict(reported.get(key, {}))
    return groups


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise the nanobot model-attempt usage ledger.")
    parser.add_argument("path", type=Path, help="model-attempts.jsonl")
    parser.add_argument("--by", default="purpose",
                        choices=["purpose", "request", "model", "provider", "status", "workspace_name"])
    parser.add_argument("--records", action="store_true", help="print the per-attempt records")
    args = parser.parse_args(argv)
    records = read_records(args.path)
    if args.records:
        for record in records:
            print(json.dumps(record, ensure_ascii=False))
    print(json.dumps({"scope": "retained provider attempts recorded by usage_ledger v1 "
                               "(not lifetime totals, not network requests, not billing)",
                     "by": args.by, "groups": summarize(records, args.by)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

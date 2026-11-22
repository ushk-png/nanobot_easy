"""Opt-in, bounded background refresh of the prompt copy, never the memory source."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from contextvars import Context
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from loguru import logger

from nanobot.agent.memory_prompt import (
    _MAX_SNAPSHOT_BYTES,
    SNAPSHOT_NAME,
    append_only_summary,
    apply_summary_edits,
    encode_snapshot,
    has_review_receipt,
    make_snapshot,
    projection_tokens,
    snapshot_summary,
)
from nanobot.providers import usage_ledger
from nanobot.providers.base import LLMProvider, hosted_tools_disabled
from nanobot.utils.prompt_templates import render_template

_MAX_SOURCE_BYTES = 128_000
_REFRESH_TIMEOUT_S = 180
_RETRY_DELAY_S = 300
_DEBOUNCE_S = 1


class MemoryRefreshError(ValueError):
    """A content-free reason code safe for the operator log."""


class MemoryPromptRefresher:
    """One job per workspace; duplicate calls never wait for it or start more work.

    The caller keeps using current raw memory until a current, in-budget snapshot
    passes a separate review. No user/session context, tools, or memory mutations
    are available to either model call. Transient failures have a five-minute cooldown.
    Exhausted quality/budget checks wait for a changed source, budget or model instead
    of buying the same failed maintenance cycle again on later chat turns.
    """

    def __init__(self, source: Path, budget: int,
                 selection: Callable[[], tuple[LLMProvider, str]]):
        self.source = source
        self.budget = budget
        self.selection = selection
        self.workspace = source.parent.parent.resolve()
        self.snapshot = source.with_name(SNAPSHOT_NAME)
        self.lock_path = source.with_name(".prompt_snapshot.lock")
        self._task: asyncio.Task | None = None
        self._closed = False
        self._last_digest: str | None = None
        self._retry_at = 0.0
        self._usage_job: usage_ledger.UsageScope | None = None
        self._validation_failure: tuple[str, int, str] | None = None
        self.status: dict = {}

    def request(self, text: str) -> None:
        if self._closed or self.budget <= 0 or not text.strip():
            return
        if self._task is not None and not self._task.done():
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self._validation_failure is not None and digest == self._validation_failure[0]:
            if self.budget == self._validation_failure[1]:
                try:
                    _, model = self.selection()
                except Exception:
                    return  # Inspection failure must not affect the foreground chat.
                if model == self._validation_failure[2]:
                    return
            self._validation_failure = None
            self._retry_at = 0.0
        if digest == self._last_digest and time.monotonic() < self._retry_at:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Offline prompt inspection has no background lifecycle.
        self._last_digest = digest
        self._retry_at = time.monotonic() + _RETRY_DELAY_S
        # Do not inherit a chat's cancellation, permission, provider budget or
        # file-state ContextVars into a separate maintenance operation.
        # Usage accounting keeps only the triggering request's opaque id, as a separate job.
        job = usage_ledger.background_job("memory_refresh", parent=usage_ledger.current_scope(),
                                          workspace=self.workspace)
        self._task = loop.create_task(self._refresh(digest, job), context=Context())

    def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()

    async def close(self) -> None:
        self.stop()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task

    def _check_paths(self) -> None:
        for path in (self.source, self.snapshot, self.lock_path):
            if path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
                raise ValueError("Memory projection paths must stay inside the workspace")
        if not self.source.is_file():
            raise ValueError("Memory source is not a regular file")

    def _read_source(self) -> str:
        self._check_paths()
        with self.source.open(encoding="utf-8") as stream:
            text = stream.read(_MAX_SOURCE_BYTES + 1)
        if not text.strip() or len(text.encode("utf-8")) > _MAX_SOURCE_BYTES:
            raise ValueError("Memory source is empty or too large for automatic refresh")
        return text

    def _read_snapshot(self) -> bytes | None:
        try:
            if not self.snapshot.is_file():
                if self.snapshot.exists():
                    raise ValueError("Snapshot is not a regular file")
                return None
            with self.snapshot.open("rb") as stream:
                raw = stream.read(_MAX_SNAPSHOT_BYTES + 1)
            if len(raw) > _MAX_SNAPSHOT_BYTES:
                raise ValueError("Snapshot is too large")
            return raw
        except FileNotFoundError:
            return None

    async def _ask(self, provider: LLMProvider, model: str, template: str,
                   payload: dict, max_tokens: int, purpose: str | None = None) -> str:
        with hosted_tools_disabled(), usage_ledger.bound(self._usage_job, purpose):
            response = await provider.chat_with_retry(
                model=model, messages=[
                    {"role": "system", "content": render_template(template, strip=True)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ], tools=None, tool_choice=None, temperature=0, max_tokens=max_tokens,
                max_retries=0, reasoning_effort="low",
            )
        if response.finish_reason != "stop":
            reason = response.finish_reason if response.finish_reason in {
                "length", "error", "content_filter", "cancelled", "tool_calls", "refusal",
            } else "unexpected"
            raise MemoryRefreshError("response_" + reason)
        if response.tool_calls:
            raise MemoryRefreshError("unexpected_tool_calls")
        if not response.content or not response.content.strip():
            raise MemoryRefreshError("empty_response")
        return response.content.strip()

    async def _refresh(self, digest: str, usage_job: usage_ledger.UsageScope | None = None) -> None:
        phase = "path checks"
        model = None
        self._usage_job = usage_job
        self.status = {"status": "running", "source_sha256": digest, "budget": self.budget}
        try:
            await asyncio.sleep(_DEBOUNCE_S)
            self._check_paths()
            # Nonblocking, cross-process locking also prevents duplicate model
            # spend when two gateways intentionally share a workspace.
            with FileLock(self.lock_path, timeout=0, mode=0o600, thread_local=False):
                async with asyncio.timeout(_REFRESH_TIMEOUT_S):
                    text = self._read_source()
                    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
                        self.status.update(status="skipped", reason="source_changed")
                        return  # A newer source will be detected on the next build.
                    before = self._read_snapshot()
                    appended = None
                    prior_summary = None
                    if before is not None:
                        try:
                            data = json.loads(before)
                            loaded = snapshot_summary(data)
                            current = make_snapshot(text, loaded, self.source, self.budget)
                            prior_summary = loaded
                            checked = has_review_receipt(data)
                            if checked and data.get("source_sha256") == current["source_sha256"]:
                                self.status.update(status="current")
                                return  # Another process already refreshed it.
                            if checked:
                                appended = append_only_summary(text, data)
                        except (ValueError, TypeError, KeyError, RecursionError):
                            pass
                    method, model = "lossless", None
                    attempts = 0
                    try:
                        use_append = (appended is not None and projection_tokens(appended, self.source)
                                      < projection_tokens(text, self.source))
                        candidate = make_snapshot(text, appended if use_append else text,
                                                  self.source, self.budget)
                        if use_append:
                            method = "append-only"
                    except ValueError:
                        provider, model = self.selection()
                        template = "agent/memory_prompt_update.md" if prior_summary is not None else "agent/memory_prompt_refresh.md"
                        payload = {"source": text, "summary_token_target": max(1, int(self.budget * 0.9)),
                                   "summary_token_budget": self.budget}
                        if prior_summary is not None:
                            payload["previous_summary"] = prior_summary
                            # A valid-sized old summary needs reconciliation, not a
                            # gratuitous rewrite to an arbitrary 90% soft target.
                            payload["summary_token_target"] = self.budget
                            payload["previous_draft_tokens"] = projection_tokens(prior_summary, self.source)
                        for attempts in (1, 2):
                            phase = "generation"
                            self.status.update(attempt=attempts)
                            summary = await self._ask(provider, model, template,
                                payload, min(16_000, max(2048, self.budget * 2)),
                                purpose="memory_refresh_generate")
                            if prior_summary is not None:
                                try:
                                    summary = apply_summary_edits(payload["previous_summary"], json.loads(summary))
                                except (ValueError, TypeError, RecursionError) as exc:
                                    raise MemoryRefreshError("invalid_summary_edits") from exc
                            phase = "budget"
                            tokens = projection_tokens(summary, self.source)
                            self.status.update(summary_tokens=tokens)
                            if tokens > self.budget:
                                logger.info("Memory prompt candidate over budget: tokens={} budget={} attempt={}",
                                            tokens, self.budget, attempts)
                                if attempts == 2:
                                    raise MemoryRefreshError("summary_over_budget")
                                feedback = [f"The draft measures {tokens} local input tokens including its notice; "
                                            f"the hard budget is {self.budget}. Compress historical detail and "
                                            "repetition further; do not remove active constraints."]
                            else:
                                candidate = make_snapshot(text, summary, self.source, self.budget)
                                phase = "review"
                                review = json.loads(await self._ask(provider, model,
                                    "agent/memory_prompt_review.md", {"source": text, "summary": summary}, 2048,
                                    purpose="memory_refresh_review"))
                                checks = ("constraints_preserved", "faithful", "no_new_authority")
                                if (not isinstance(review, dict)
                                        or any(type(review.get(k)) is not bool for k in checks)
                                        or not isinstance(review.get("issues"), list)):
                                    raise MemoryRefreshError("invalid_review")
                                self.status["review"] = {k: review[k] for k in checks}
                                self.status["review"]["issue_count"] = len(review["issues"])
                                if all(review[k] for k in checks) and review["issues"] == []:
                                    break
                                feedback = review["issues"]
                                if (attempts == 2 or not feedback
                                        or not all(isinstance(s, str) for s in feedback)
                                        or sum(map(len, feedback)) > 4000):
                                    raise MemoryRefreshError("review_rejected")
                            if self._read_source() != text or self._read_snapshot() != before:
                                raise MemoryRefreshError("source_or_snapshot_changed")
                            logger.info("Memory prompt revising candidate once: phase={}", phase)
                            # The update template edits previous_summary; the full-generation
                            # template revises previous_draft. Send the same candidate only once.
                            draft_key = "previous_summary" if prior_summary is not None else "previous_draft"
                            payload = {"source": text, draft_key: summary, "review_feedback": feedback,
                                       "previous_draft_tokens": tokens, "summary_token_budget": self.budget,
                                       "summary_token_target": max(1, int(self.budget * 0.85))}
                        method = "model-reviewed"
                    phase = "publication"
                    candidate["refresh"] = {"method": method, "model": model,
                        "created_at": datetime.now(timezone.utc).isoformat(), "attempts": attempts,
                        "summary_tokens": projection_tokens(candidate["summary"], self.source),
                        "reviewed_source_sha256": candidate["source_sha256"],
                        "reviewed_summary_sha256": hashlib.sha256(candidate["summary"].encode("utf-8")).hexdigest()}
                    raw = encode_snapshot(candidate)
                    if len(raw) > _MAX_SNAPSHOT_BYTES:
                        raise ValueError("Candidate file is too large")
                    self._publish(text, before, raw)
                    self._validation_failure = None
                    self.status.update(status="published", phase=phase,
                                       summary_tokens=candidate["refresh"]["summary_tokens"])
                    logger.info("Memory prompt refreshed: method={} source_sha256={} tokens={} attempts={}",
                                method, digest, candidate["refresh"]["summary_tokens"], attempts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider/parser errors may contain memory text or credentials.
            self._retry_at = time.monotonic() + _RETRY_DELAY_S
            reason = str(exc) if isinstance(exc, MemoryRefreshError) else type(exc).__name__
            validation_failed = reason in {"summary_over_budget", "review_rejected", "response_length"} and model is not None
            if validation_failed:
                self._validation_failure = (digest, self.budget, model)
            retry = "source_budget_or_model_change" if validation_failed else "cooldown"
            self.status.update(status="deferred", phase=phase, reason=reason, retry=retry)
            logger.warning("Memory prompt refresh deferred: phase={} reason={}; "
                           "keeping current raw memory; retry={} cooldown={}s",
                           phase, reason, retry, _RETRY_DELAY_S)

    def _publish(self, text: str, before: bytes | None, raw: bytes) -> None:
        fd, name = tempfile.mkstemp(prefix=".memory-prompt-", dir=self.source.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # A manual replacement wins; a source edit invalidates this whole job.
            if self._read_source() != text or self._read_snapshot() != before:
                raise ValueError("Memory or snapshot changed while refreshing")
            os.replace(temporary, self.snapshot)
            if os.name != "nt":
                directory = os.open(self.source.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

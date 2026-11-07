"""t54: one whole-request budget shared by every generation send of a request.

Attached to the request's ContextTrace, so the first design, its repair, executor iterations, the
provider's internal retries, plan changes / supplements, returns after a failed final verification or
an unsupported change claim, and final-answer fixes all draw from the same counters. It is checked and
decremented immediately before each actual send (LLMProvider._safe_chat / _safe_chat_stream); the check
and the decrement happen under one lock with no await in between, so concurrent sends cannot overshoot.

Limits (all optional; None = not set by this module):
  max_sends      generation sends (attempts, including internal retries)
  deadline_s     wall-clock seconds from the request start; caps model/tool/retry waits as
                 min(per-operation limit, remaining)
  max_tokens     enforced only while every response so far reported usage; an unreported usage is
                 never counted as 0 (the token cap then becomes "unenforceable" and calls/time still apply)
Per-requirement no-progress limits stay in context_plan (separate; progress there never resets these).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

STOP_REASONS = {"budget_sends", "budget_deadline", "budget_tokens", "no_progress_repeat"}


def now() -> float:            # the module clock (tests substitute a controllable one)
    return time.monotonic()


class TaskBudget:
    def __init__(self, request_id: str, max_sends: int | None = None, deadline_s: float | None = None,
                 max_tokens: int | None = None, clock: Callable[[], float] | None = None,
                 prior: dict | None = None):
        self.request_id = request_id
        self.max_sends, self.deadline_s, self.max_tokens = max_sends, deadline_s, max_tokens
        self._clock = clock or now
        self.started = self._clock()
        self.sends = 0
        self.refused = 0
        self.non_generation = 0
        self.tokens_known = 0
        self.unknown_usage = 0
        self.elapsed_before = 0.0
        self.stop_reason: str | None = None
        self.segments: list[dict] = []
        self.sources = {"calls": "explicit" if max_sends is not None else "unset",
                        "time": "explicit" if deadline_s is not None else "unset",
                        "tokens": "explicit" if max_tokens is not None else "unset"}
        self._lock = threading.Lock()
        if prior:
            self.restore(prior)

    # ---- state -------------------------------------------------------------------------------------
    def elapsed(self) -> float:
        return self.elapsed_before + max(0.0, self._clock() - self.started)

    def remaining_s(self) -> float | None:
        if self.deadline_s is None:
            return None
        return max(0.0, self.deadline_s - self.elapsed())

    def cap(self, per_op: float | None) -> float | None:
        """min(per-operation limit, remaining whole-request time); None = no limit at all."""
        rem = self.remaining_s()
        if rem is None:
            return per_op
        return rem if per_op is None else min(per_op, rem)

    def token_cap_enforceable(self) -> bool:
        return self.max_tokens is not None and self.unknown_usage == 0

    def exhausted(self) -> str | None:
        if self.stop_reason:
            return self.stop_reason
        if self.max_sends is not None and self.sends >= self.max_sends:
            return "budget_sends"
        if self.deadline_s is not None and self.remaining_s() <= 0:
            return "budget_deadline"
        if self.token_cap_enforceable() and self.tokens_known >= self.max_tokens:
            return "budget_tokens"
        return None

    # ---- the send gate -------------------------------------------------------------------------------
    def acquire(self) -> str | None:
        """Check + decrement atomically right before one actual generation send. Returns the stop
        reason when refused (nothing is sent)."""
        with self._lock:
            reason = self.exhausted()
            if reason:
                self.refused += 1
                self.stop_reason = self.stop_reason or reason
                return reason
            self.sends += 1
            return None

    def record_usage(self, usage: Any) -> None:
        total = None
        if isinstance(usage, dict) and usage:
            total = usage.get("total_tokens")
            if total is None and ("prompt_tokens" in usage or "completion_tokens" in usage):
                total = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        with self._lock:
            if total is None:
                self.unknown_usage += 1          # unknown is not 0
            else:
                self.tokens_known += int(total)

    def note_non_generation(self) -> None:
        """A non-generation request (e.g. a model list): counted apart; its time is in elapsed()."""
        with self._lock:
            self.non_generation += 1

    def stop(self, reason: str) -> None:
        with self._lock:
            self.stop_reason = self.stop_reason or reason

    def set_default_sends(self, n: int, source: str) -> None:
        if self.max_sends is None:
            self.max_sends, self.sources["calls"] = n, "default:" + source

    def start_record(self) -> dict:
        """t55: what actually applies to this request. A missing time cap is reported as no whole
        deadline, never as time-protected."""
        return {"call_cap": self.max_sends, "call_cap_source": self.sources["calls"],
                "time_cap_s": self.deadline_s,
                "time_cap": "전체 마감 없음" if self.deadline_s is None else str(self.deadline_s) + "초",
                "time_cap_source": self.sources["time"], "deadline_applied": self.deadline_s is not None,
                "token_cap": ("unset" if self.max_tokens is None else
                              "enforced" if self.token_cap_enforceable() else "unenforceable_unknown_usage")}

    # ---- persistence (existing task record) ------------------------------------------------------------
    def snapshot(self) -> dict:
        return {"request_id": self.request_id, "sends": self.sends, "refused": self.refused,
                "non_generation": self.non_generation, "tokens_known": self.tokens_known,
                "unknown_usage_calls": self.unknown_usage,
                "token_cap": ("unset" if self.max_tokens is None else
                              "enforced" if self.token_cap_enforceable() else "unenforceable_unknown_usage"),
                "elapsed_s": round(self.elapsed(), 3), "max_sends": self.max_sends,
                "deadline_s": self.deadline_s, "stop_reason": self.stop_reason, "segments": list(self.segments),
                "start": self.start_record()}

    def restore(self, prior: dict, same_request: bool = False) -> None:
        """Same request (process resume / loop re-entry / state restore): consumption continues, no new
        budget. A different request on the same task after a stop is a new segment (see next_segment)."""
        if same_request or prior.get("request_id") == self.request_id:
            # additive: sends made in this pass before the restore stay counted
            self.sends += int(prior.get("sends") or 0)
            self.refused += int(prior.get("refused") or 0)
            self.non_generation += int(prior.get("non_generation") or 0)
            self.tokens_known += int(prior.get("tokens_known") or 0)
            self.unknown_usage += int(prior.get("unknown_usage_calls") or 0)
            self.elapsed_before += float(prior.get("elapsed_s") or 0.0)
            self.stop_reason = self.stop_reason or prior.get("stop_reason")
            self.segments = list(prior.get("segments") or [])
            self.request_id = prior.get("request_id") or self.request_id   # internal continuation keeps it
        else:
            # A new user request: a new segment; earlier consumption is kept as history, not erased.
            past = {k: prior.get(k) for k in ("request_id", "sends", "elapsed_s", "tokens_known",
                                              "unknown_usage_calls", "stop_reason")}
            self.segments = [*(prior.get("segments") or []), past][-20:]

    def total_sends(self) -> int:
        return self.sends + sum(int(s.get("sends") or 0) for s in self.segments)


def require_explicit_caps(config) -> None:
    """t55 verification entry point: refuse to run unless BOTH a finite generation-send cap and a finite
    whole-request time cap are set explicitly. Does not touch any existing user configuration."""
    attempts = getattr(config, "task_generation_attempts", None)
    deadline = getattr(config, "task_deadline_seconds", None)
    if not (isinstance(attempts, int) and not isinstance(attempts, bool) and attempts > 0):
        raise ValueError("explicit finite task_generation_attempts required (got %r)" % (attempts,))
    if not (isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and 0 < deadline < float("inf")):
        raise ValueError("explicit finite task_deadline_seconds required (got %r)" % (deadline,))


def active_budget() -> TaskBudget | None:
    from nanobot.agent.context_trace import active_context_trace
    trace = active_context_trace.get()
    return getattr(trace, "budget", None) if trace is not None else None

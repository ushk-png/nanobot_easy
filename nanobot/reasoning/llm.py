"""Narrow stage-call boundary.

The adapter receives only the current stage contract and returns structured
judgments. It cannot change procedure flow or mark a check as verified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class BudgetExceededError(Exception):
    """A run used up its time or model-call budget (design 9, 12)."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind} budget exceeded{': ' + detail if detail else ''}")
        self.kind = kind
        self.detail = detail


@dataclass
class RunBudget:
    """Per-run budget shared by routing, step, review and retry calls."""

    seconds: float | None = 120.0
    max_calls: int | None = 8
    started: float = field(default_factory=time.monotonic)
    calls: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def before_call(self, stage: str) -> float | None:
        """Count the call and return the seconds left for it (None = no time limit)."""

        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceededError("calls", f"{self.max_calls} model calls used before {stage}")
        remaining = None
        if self.seconds is not None:
            remaining = self.seconds - self.elapsed()
            if remaining <= 0:
                raise BudgetExceededError("time", f"{self.seconds:.0f}s used before {stage}")
        self.calls += 1
        return remaining


class StageLLM(Protocol):
    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a structured stage result. Token counts are optional."""


class ScriptedStageLLM:
    """Deterministic adapter for tests and offline runs."""

    def __init__(self, scripts: dict[str, dict[str, Any]]) -> None:
        self.scripts = scripts
        self.calls: list[str] = []

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        self.calls.append(stage)
        if stage not in self.scripts:
            raise KeyError(f"no script for stage {stage}")
        return dict(self.scripts[stage])

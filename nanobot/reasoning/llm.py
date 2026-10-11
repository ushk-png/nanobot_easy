"""Narrow stage-call boundary.

The adapter receives only the current stage contract and returns structured
judgments. It cannot change procedure flow or mark a check as verified.
"""

from __future__ import annotations

from typing import Any, Protocol


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

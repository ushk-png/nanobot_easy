"""Privacy-safe per-provider-attempt accounting for selective context turns."""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from nanobot.utils.helpers import estimate_message_tokens


def estimate_component(value: Any) -> int:
    """Use the repository estimator with its single-message contract."""
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

    def count(self, name: str, reason: str | None = None) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1
        if reason and reason not in self.reasons:
            self.reasons.append(reason)

    def record(self, kwargs: dict[str, Any], response: Any, elapsed: float) -> None:
        self.calls.append({
            "request_id": self.request_id, "task_id": self.task_id, "stage": self.stage,
            "call_sequence": len(self.calls) + 1,
            "estimated_input_tokens": estimate_component(kwargs.get("messages", []))
                + estimate_component(kwargs.get("tools") or []),
            "estimated_output_tokens": estimate_component(response.content or "")
                + estimate_component([getattr(t, "arguments", {}) for t in response.tool_calls])
                + estimate_component(getattr(response, "reasoning_content", None) or ""),
            "provider_usage": {k: v for k, v in (response.usage or {}).items()
                               if isinstance(v, int) and not isinstance(v, bool)}, "elapsed_ms": int(elapsed * 1000),
            "error": response.finish_reason == "error",
        })

    def report(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "task_id": self.task_id,
                "calls": self.calls, "counters": self.counters, "reasons": self.reasons,
                "component_estimated_tokens": self.components,
                "elapsed_ms": int((time.monotonic() - self.started) * 1000),
                "estimated_total_tokens": sum(c["estimated_input_tokens"]
                    + c["estimated_output_tokens"] for c in self.calls),
                "provider_usage_calls": sum(bool(c["provider_usage"]) for c in self.calls),
                "provider_usage_missing_calls": sum(not c["provider_usage"] for c in self.calls),
                "provider_total_tokens": sum(c["provider_usage"].get("total_tokens", 0)
                    or (c["provider_usage"].get("prompt_tokens", 0)
                        + c["provider_usage"].get("completion_tokens", 0)) for c in self.calls)}

    def persist(self, store: Any) -> None:
        with store.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO context_selection_runs VALUES (?, ?, ?)",
                         (self.request_id, self.task_id, json.dumps(self.report())))


active_context_trace: ContextVar[ContextTrace | None] = ContextVar("context_trace", default=None)

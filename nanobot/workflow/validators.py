"""Pure validation helpers for workflow review steps."""
from __future__ import annotations

from typing import Any, Callable

Validator = Callable[[Any, dict[str, Any]], str | None]


def required_field(value: Any, config: dict[str, Any]) -> str | None:
    field = str(config.get("field") or "")
    if not isinstance(value, dict):
        return "validator required_field expects object value"
    if not field or value.get(field) in (None, ""):
        return f"missing required field: {field}"
    return None


def non_empty_text(value: Any, config: dict[str, Any]) -> str | None:
    if isinstance(value, dict):
        field = str(config.get("field") or "answer")
        value = value.get(field)
    if not isinstance(value, str) or not value.strip():
        return "text must be non-empty"
    return None


def allowed_review_decision(value: Any, config: dict[str, Any]) -> str | None:
    field = str(config.get("field") or "decision")
    allowed = set(config.get("allowed") or ["pass", "rewrite", "need_evidence", "limit_exceeded"])
    if not isinstance(value, dict):
        return "validator allowed_review_decision expects object value"
    decision = value.get(field)
    if decision not in allowed:
        return f"review decision {decision!r} not allowed"
    return None


_VALIDATORS: dict[str, Validator] = {
    "required_field": required_field,
    "non_empty_text": non_empty_text,
    "allowed_review_decision": allowed_review_decision,
}


def run_validators(names: list[str], value: Any, config: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    cfg = config or {}
    for name in names:
        validator = _VALIDATORS.get(name)
        if validator is None:
            errors.append(f"unknown workflow validator: {name}")
            continue
        error = validator(value, cfg)
        if error:
            errors.append(error)
    return errors

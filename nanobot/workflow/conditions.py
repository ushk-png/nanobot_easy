"""Restricted workflow condition helpers.

Stage 1 provides the safe evaluator contract used by later branch execution.
No Python eval/exec is used here.
"""
from __future__ import annotations

from typing import Any


class ConditionError(ValueError):
    pass


def resolve_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not part:
            raise ConditionError("empty path segment")
        if not isinstance(current, dict) or part not in current:
            raise ConditionError(f"missing path: {path}")
        current = current[part]
    return current


def evaluate_condition(expr: dict[str, Any], data: dict[str, Any]) -> bool:
    op = expr.get("op")
    if op == "exists":
        try:
            resolve_path(data, str(expr.get("path") or ""))
            return True
        except ConditionError:
            return False
    if op in {"eq", "ne", "in", "gt", "gte", "lt", "lte"}:
        left = resolve_path(data, str(expr.get("path") or ""))
        right = expr.get("value")
        if op == "eq":
            return left == right
        if op == "ne":
            return left != right
        if op == "in":
            return left in right if isinstance(right, list | tuple | set) else False
        if not isinstance(left, int | float) or not isinstance(right, int | float):
            raise ConditionError(f"numeric comparison requires numbers: {op}")
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right
    if op == "and":
        items = expr.get("conditions")
        if not isinstance(items, list):
            raise ConditionError("and requires conditions list")
        return all(evaluate_condition(item, data) for item in items)
    if op == "or":
        items = expr.get("conditions")
        if not isinstance(items, list):
            raise ConditionError("or requires conditions list")
        return any(evaluate_condition(item, data) for item in items)
    if op == "not":
        item = expr.get("condition")
        if not isinstance(item, dict):
            raise ConditionError("not requires condition object")
        return not evaluate_condition(item, data)
    raise ConditionError(f"unsupported condition op: {op!r}")

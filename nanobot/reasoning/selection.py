"""Deterministic filter-and-select over an extracted candidate table.

Design 8: counts, ranges and fixed comparisons are code work. The model only
extracts the table (candidates, required conditions, objective) and explains
the computed result. An unconfirmed value (null) never satisfies a condition.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from typing import Any

_NUMERIC_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}
_EQUALITY_OPS = {"==", "!="}
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def constraint_select(table: Any) -> tuple[dict[str, Any], list[str]]:
    """Return (selection, defects). Defects mean the table must be re-extracted."""

    if not isinstance(table, dict):
        return {}, ["table output has an invalid shape"]
    candidates = table.get("candidates")
    required = table.get("required") or []
    objective = table.get("objective") or None
    defects = _table_defects(candidates, required, objective)
    if defects:
        return {}, defects

    per_candidate: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in candidates:
        candidate_id = str(raw["id"]).strip()
        values = raw.get("values") or {}
        failed: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        for condition in required:
            actual = values.get(condition["attr"])
            result = _compare(actual, condition["op"], condition["value"])
            entry = {"condition": _describe(condition), "value": actual}
            if result is None:
                unknown.append(entry)
            elif not result:
                failed.append(entry)
        status = "failed" if failed else "unknown" if unknown else "eligible"
        per_candidate[candidate_id] = {"status": status, "failed": failed, "unknown": unknown}
        order.append(candidate_id)

    eligible = [item for item in order if per_candidate[item]["status"] == "eligible"]
    selection: dict[str, Any] = {
        "selected": None,
        "reason": "",
        "eligible": eligible,
        "ties": [],
        "objective": objective,
        "candidates": per_candidate,
    }
    if not eligible:
        selection["reason"] = "no candidate meets every required condition with confirmed values"
        return selection, []
    if objective:
        attr, direction = objective["attr"], objective.get("direction", "max")
        by_id = {str(raw["id"]).strip(): raw.get("values") or {} for raw in candidates}
        scored = []
        for item in eligible:
            number = _number(by_id[item].get(attr))
            per_candidate[item]["objective_value"] = number
            if number is not None:
                scored.append((number, item))
        if not scored:
            selection["reason"] = f"objective {attr} is unknown for every eligible candidate"
            return selection, []
        best = max(value for value, _ in scored) if direction == "max" else min(value for value, _ in scored)
        ties = [item for value, item in scored if value == best]
        selection.update(
            selected=ties[0],
            ties=ties if len(ties) > 1 else [],
            reason=f"{direction} {attr} among eligible candidates",
        )
        return selection, []
    if len(eligible) == 1:
        selection.update(selected=eligible[0], reason="only candidate meeting every required condition")
    else:
        selection["reason"] = "several candidates qualify and no objective was given"
    return selection, []


def _table_defects(candidates: Any, required: Any, objective: Any) -> list[str]:
    defects: list[str] = []
    if not isinstance(candidates, list) or not candidates:
        return ["table has no candidates"]
    ids: list[str] = []
    attrs: set[str] = set()
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
            defects.append(f"candidates[{index}] needs an id")
            continue
        if not isinstance(raw.get("values") or {}, dict):
            defects.append(f"candidates[{index}].values must be an object")
            continue
        ids.append(str(raw["id"]).strip())
        attrs.update(str(key) for key in (raw.get("values") or {}))
    if len(set(ids)) != len(ids):
        defects.append("candidate ids must be unique")
    if not isinstance(required, list):
        return defects + ["required must be a list"]
    if not required and not objective:
        defects.append("table needs required conditions or an objective")
    for index, condition in enumerate(required):
        if not isinstance(condition, dict):
            defects.append(f"required[{index}] must be an object")
            continue
        attr, op = str(condition.get("attr") or ""), str(condition.get("op") or "")
        if op not in _NUMERIC_OPS:
            defects.append(f"required[{index}].op must be one of {', '.join(_NUMERIC_OPS)}")
        if "value" not in condition or condition.get("value") is None:
            defects.append(f"required[{index}] needs a comparison value")
        if attr not in attrs:
            defects.append(f"required[{index}].attr '{attr}' is not a candidate value name")
    if objective is not None:
        if not isinstance(objective, dict) or str(objective.get("attr") or "") not in attrs:
            defects.append("objective.attr must be a candidate value name")
        elif objective.get("direction", "max") not in {"max", "min"}:
            defects.append("objective.direction must be max or min")
    return defects


def _compare(actual: Any, op: str, target: Any) -> bool | None:
    """True/False when decidable; None when the value is unconfirmed or not comparable."""

    if actual is None:
        return None
    expected = _as_bool(target)
    if expected is not None:
        value = _as_bool(actual)
        if value is None or op not in _EQUALITY_OPS:
            return None
        return _NUMERIC_OPS[op](value, expected)
    target_number, actual_number = _number(target), _number(actual)
    if target_number is not None:
        if actual_number is None:
            return None
        return _NUMERIC_OPS[op](actual_number, target_number)
    if op not in _EQUALITY_OPS:
        return None
    return _NUMERIC_OPS[op](str(actual).strip(), str(target).strip())


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.replace(",", "").replace(" ", "")
        if _NUMBER_RE.fullmatch(text):
            return float(text)
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _describe(condition: dict[str, Any]) -> str:
    return f"{condition['attr']} {condition['op']} {condition['value']}"


CODE_OPS: dict[str, Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]] = {
    "code:constraint_select": lambda inputs: _selection_op(inputs),
}


def _selection_op(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    selection, defects = constraint_select(inputs.get("table"))
    return ({"selection": selection} if selection else {}), defects

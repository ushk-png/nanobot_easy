"""Validate a one-time procedure proposed by the LLM.

Passing validation permits immediate execution only. It never adds the
procedure to the approved reusable registry.
"""

from __future__ import annotations

from typing import Any

from nanobot.reasoning.contract import (
    ContractDefect,
    normalize_procedure,
    validate_normalized_procedure,
)
from nanobot.reasoning.models import Procedure

MAX_DYNAMIC_RECOVERY = 2


def validate_dynamic_procedure(
    payload: dict[str, Any] | None,
    *,
    request: str,
    known_goal: str = "",
    procedure_id: str | None = None,
    revision: int = 1,
) -> tuple[Procedure | None, list[str]]:
    """Normalize, fill code-owned fields, then check the shared contract."""

    procedure, _trace, defects = prepare_dynamic_procedure(
        payload,
        request=request,
        known_goal=known_goal,
        procedure_id=procedure_id,
        revision=revision,
    )
    return procedure, [item.message() for item in defects]


def prepare_dynamic_procedure(
    payload: dict[str, Any] | None,
    *,
    request: str,
    known_goal: str = "",
    procedure_id: str | None = None,
    revision: int = 1,
) -> tuple[Procedure | None, dict[str, Any], list[ContractDefect]]:
    normalized, record, defects = normalize_procedure(
        payload,
        request=request,
        known_goal=known_goal,
        procedure_id=procedure_id,
        revision=revision,
    )
    if defects or normalized is None:
        return None, {"normalization": record.__dict__}, defects
    procedure, contract_defects = validate_normalized_procedure(
        normalized,
        request=request,
        known_goal=known_goal,
    )
    trace = {
        "normalization": record.__dict__,
        "normalized": normalized,
        "revision": procedure.revision if procedure else revision,
    }
    return procedure, trace, contract_defects


def recovery_budget_exceeded(attempts: int) -> bool:
    return attempts >= MAX_DYNAMIC_RECOVERY

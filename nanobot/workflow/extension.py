"""Stage-6 optional workflow extension harness.

This module does not auto-generate Python functions or mutate the live runtime
registry.  It provides contracts for candidate function/tool versions, validates
candidate quality evidence, and applies approved candidates to a disposable
registry snapshot with rollback support.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanobot.agent.tools.base import Schema, Tool
from nanobot.agent.tools.registry import ToolRegistry

CandidateStatus = str

_ALLOWED_STATUSES = {"draft", "verified", "rejected"}
_FORBIDDEN_NAMES = {"workflow", "spawn", "delegate"}


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    normal_cases: int = 0
    error_cases: int = 0
    boundary_cases: int = 0
    integration_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total_cases(self) -> int:
        return self.passed_cases + self.failed_cases

    @property
    def ok(self) -> bool:
        return (
            self.failed_cases == 0
            and self.passed_cases > 0
            and self.normal_cases > 0
            and self.error_cases > 0
            and self.boundary_cases > 0
            and self.integration_cases > 0
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "normal_cases": self.normal_cases,
            "error_cases": self.error_cases,
            "boundary_cases": self.boundary_cases,
            "integration_cases": self.integration_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "notes": list(self.notes),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class FunctionCandidate:
    name: str
    version: str
    purpose: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    preconditions: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    external_side_effects: bool = False
    retryable: bool = False
    evaluation: CandidateEvaluation = field(default_factory=CandidateEvaluation)
    status: CandidateStatus = "draft"
    implementation: Tool | None = None

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def model_dump(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "purpose": self.purpose,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "preconditions": list(self.preconditions),
            "error_types": list(self.error_types),
            "permissions": list(self.permissions),
            "external_side_effects": self.external_side_effects,
            "retryable": self.retryable,
            "evaluation": self.evaluation.model_dump(),
            "status": self.status,
            "has_implementation": self.implementation is not None,
        }


@dataclass(frozen=True, slots=True)
class CandidateValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    candidate_key: str
    tool_name: str
    previous_tool_present: bool
    promoted: bool
    reason: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "tool_name": self.tool_name,
            "previous_tool_present": self.previous_tool_present,
            "promoted": self.promoted,
            "reason": self.reason,
        }


class WorkflowCandidateRegistry:
    """In-memory candidate ledger for optional stage-6 extensions."""

    def __init__(self) -> None:
        self._candidates: dict[str, FunctionCandidate] = {}

    def add(self, candidate: FunctionCandidate) -> CandidateValidationResult:
        validation = validate_candidate(candidate)
        if not validation.ok:
            return validation
        self._candidates[candidate.key] = candidate
        return validation

    def get(self, key: str) -> FunctionCandidate | None:
        return self._candidates.get(key)

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        candidates = self._candidates.values()
        if status is not None:
            candidates = [c for c in candidates if c.status == status]
        return [candidate.model_dump() for candidate in sorted(candidates, key=lambda c: c.key)]

    def verified(self) -> list[FunctionCandidate]:
        return [candidate for candidate in self._candidates.values() if candidate.status == "verified"]


def validate_candidate(candidate: FunctionCandidate) -> CandidateValidationResult:
    errors: list[str] = []
    if not candidate.name or not candidate.name.replace("_", "").isalnum():
        errors.append("candidate name must be non-empty and use alphanumeric/underscore characters")
    if candidate.name in _FORBIDDEN_NAMES:
        errors.append(f"candidate name {candidate.name!r} is reserved")
    if not candidate.version:
        errors.append("candidate version is required")
    if not candidate.purpose.strip():
        errors.append("candidate purpose is required")
    if candidate.status not in _ALLOWED_STATUSES:
        errors.append(f"candidate status must be one of {sorted(_ALLOWED_STATUSES)}")
    if candidate.implementation is not None and candidate.implementation.name != candidate.name:
        errors.append("candidate implementation tool name must match candidate name")
    errors.extend(_validate_schema(candidate.input_schema, "input_schema"))
    errors.extend(_validate_schema(candidate.output_schema, "output_schema"))
    if candidate.status == "verified":
        if candidate.implementation is None:
            errors.append("verified candidate requires an implementation tool")
        if not candidate.evaluation.ok:
            errors.append("verified candidate requires passing normal, error, boundary, and integration evidence")
        if candidate.external_side_effects and not candidate.permissions:
            errors.append("side-effecting candidate requires explicit permissions")
    return CandidateValidationResult(ok=not errors, errors=errors)


def _validate_schema(schema: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{label} must be a JSON schema object"]
    if "type" not in schema:
        errors.append(f"{label} requires type")
    if schema.get("type") == "object" and not isinstance(schema.get("properties", {}), dict):
        errors.append(f"{label}.properties must be an object")
    return errors


def validate_candidate_inputs(candidate: FunctionCandidate, params: dict[str, Any]) -> list[str]:
    return Schema.validate_json_schema_value(params, candidate.input_schema, "params")


def validate_candidate_output(candidate: FunctionCandidate, output: Any) -> list[str]:
    return Schema.validate_json_schema_value(output, candidate.output_schema, "output")


def registry_snapshot(registry: ToolRegistry) -> ToolRegistry:
    snapshot = ToolRegistry()
    for name in registry.tool_names:
        tool = registry.get(name)
        if tool is not None:
            snapshot.register(tool)
    return snapshot


def promote_candidate_to_registry(registry: ToolRegistry, candidate: FunctionCandidate) -> tuple[ToolRegistry, PromotionRecord]:
    """Return a registry copy with a verified candidate installed.

    The input registry is not mutated.  This makes promotion cheap to roll back
    and prevents candidate code from silently replacing operational tools.
    """

    validation = validate_candidate(candidate)
    if not validation.ok or candidate.status != "verified":
        reason = "; ".join(validation.errors)
        if candidate.status != "verified":
            reason = (reason + "; " if reason else "") + "candidate status must be verified before promotion"
        return registry_snapshot(registry), PromotionRecord(
            candidate_key=candidate.key,
            tool_name=candidate.name,
            previous_tool_present=registry.has(candidate.name),
            promoted=False,
            reason=reason,
        )
    assert candidate.implementation is not None
    promoted = registry_snapshot(registry)
    previous = promoted.has(candidate.name)
    promoted.register(candidate.implementation)
    return promoted, PromotionRecord(
        candidate_key=candidate.key,
        tool_name=candidate.name,
        previous_tool_present=previous,
        promoted=True,
        reason="verified candidate promoted to registry snapshot",
    )


def rollback_registry(_promoted_registry: ToolRegistry, original_registry: ToolRegistry) -> ToolRegistry:
    """Return a fresh copy of the original registry, discarding a promotion."""

    return registry_snapshot(original_registry)

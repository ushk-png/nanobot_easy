"""Shared execution contract for approved and dynamic procedures.

Code owns identity and display metadata. The model owns purpose, step work,
and data links. S1-S7 is one supported template, not a required shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from nanobot.reasoning.models import Procedure, ProcedureStep, StepBudget

SCHEMA_VERSION = "3.1"
_STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_PROCEDURE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TERM_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_KNOWN_WRAPPERS = ("procedure", "plan", "result", "output")
_LEGACY_VERSIONS = {"3.0"}
_ROLE_OUTPUTS = {
    "goal": "goal",
    "facts": "facts",
    "criteria": "criteria",
    "judgments": "judgments",
    "conclusion": "conclusion",
    "check": "checks",
    "answer": "answer",
}
_CANONICAL_ROLES = (
    "goal",
    "facts",
    "criteria",
    "judgments",
    "conclusion",
    "check",
    "answer",
)


@dataclass
class ContractDefect:
    code: str
    location: str
    expected: str
    observed: str
    recoverable: bool

    def message(self) -> str:
        return f"{self.code} at {self.location}: expected {self.expected}; observed {self.observed}"


@dataclass
class NormalizationRecord:
    before_keys: list[str] = field(default_factory=list)
    after_keys: list[str] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)
    extracted_from: str = ""
    converted_from: str = ""
    conflicts: list[str] = field(default_factory=list)
    revision: int = 1


def parse_procedure_payload(content: str) -> tuple[dict[str, Any] | None, list[ContractDefect]]:
    """Separate truncated or non-object output from a missing procedure."""

    import json

    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        return None, [
            ContractDefect(
                "parse_failed",
                "response",
                "one JSON object",
                "no object" if content.strip() else "empty response",
                True,
            )
        ]
    fragment = content[start : end + 1]
    try:
        value = json.loads(fragment)
    except json.JSONDecodeError as exc:
        return None, [
            ContractDefect("parse_failed", "response", "complete JSON", exc.msg, True)
        ]
    if not isinstance(value, dict):
        return None, [
            ContractDefect("parse_failed", "response", "JSON object", type(value).__name__, True)
        ]
    return value, []


def normalize_procedure(
    payload: dict[str, Any] | None,
    *,
    request: str,
    known_goal: str = "",
    procedure_id: str | None = None,
    revision: int = 1,
) -> tuple[dict[str, Any] | None, NormalizationRecord, list[ContractDefect]]:
    """Normalize known shapes and fill only code-owned fields."""

    record = NormalizationRecord(revision=revision)
    if not isinstance(payload, dict):
        return None, record, [
            ContractDefect("parse_failed", "payload", "object", type(payload).__name__, True)
        ]
    record.before_keys = sorted(str(key) for key in payload)
    extracted, source = _extract_known(payload)
    record.extracted_from = source
    if extracted is None:
        return None, record, [
            ContractDefect(
                "ambiguous_object",
                "payload",
                "one procedure object",
                f"multiple candidates: {source}",
                True,
            )
        ]

    normalized, conversion_defects = _convert_known_legacy(extracted, record)
    if conversion_defects:
        record.after_keys = sorted(str(key) for key in normalized)
        return None, record, conversion_defects
    raw_steps = normalized.get("steps")
    if isinstance(raw_steps, list):
        steps, step_defects = _normalize_steps(raw_steps)
        if step_defects:
            record.after_keys = sorted(str(key) for key in normalized)
            return None, record, step_defects
        normalized["steps"] = steps

    if not str(normalized.get("id") or "").strip():
        normalized["id"] = procedure_id or f"dynamic-{uuid4().hex[:12]}"
        record.filled.append("id")
    elif procedure_id and str(normalized.get("id")).strip() != procedure_id:
        normalized["id"] = procedure_id
        record.filled.append("id")
    if not str(normalized.get("title") or "").strip():
        normalized["title"] = _default_title(request, str(normalized["id"]))
        record.filled.append("title")
    purpose = str(normalized.get("purpose") or "").strip()
    goal = known_goal.strip()
    if not purpose and goal:
        normalized["purpose"] = goal
        record.filled.append("purpose")
    elif purpose and goal and purpose != goal:
        record.conflicts.append("purpose")
        record.after_keys = sorted(str(key) for key in normalized)
        return None, record, [
            ContractDefect(
                "purpose_conflict",
                "procedure.purpose",
                "confirmed goal or empty purpose",
                "different purpose",
                True,
            )
        ]
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["revision"] = revision
    record.after_keys = sorted(str(key) for key in normalized)
    return normalized, record, []


def validate_normalized_procedure(
    payload: dict[str, Any] | None,
    *,
    request: str,
    known_goal: str = "",
) -> tuple[Procedure | None, list[ContractDefect]]:
    """Validate the shared contract without requiring seven S-steps."""

    if not isinstance(payload, dict):
        return None, [
            ContractDefect("parse_failed", "procedure", "object", "missing", False)
        ]
    defects: list[ContractDefect] = []
    procedure_id = str(payload.get("id") or "").strip()
    if not _PROCEDURE_ID_RE.fullmatch(procedure_id):
        defects.append(
            ContractDefect("invalid_id", "procedure.id", "lowercase slug", procedure_id or "missing", False)
        )
    purpose = str(payload.get("purpose") or "").strip()
    if not purpose:
        defects.append(ContractDefect("purpose_missing", "procedure.purpose", "confirmed goal", "missing", True))
    elif not _purpose_fits(request, purpose) and not _purpose_fits(known_goal, purpose):
        defects.append(
            ContractDefect("purpose_unfit", "procedure.purpose", "terms from the request", purpose, True)
        )

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        defects.append(ContractDefect("steps_missing", "procedure.steps", "one or more steps", "missing", True))
        return None, defects

    steps: list[ProcedureStep] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps):
        location = f"steps[{index}]"
        if not isinstance(raw, dict):
            defects.append(ContractDefect("step_invalid", location, "object", type(raw).__name__, True))
            continue
        step_id = str(raw.get("id") or "").strip()
        if not _STEP_ID_RE.fullmatch(step_id):
            defects.append(ContractDefect("step_id_invalid", f"{location}.id", "unique step id", step_id or "missing", True))
            continue
        if step_id in seen:
            defects.append(ContractDefect("duplicate_step_id", f"{location}.id", "unique step id", step_id, True))
            continue
        seen.add(step_id)
        question = str(raw.get("question") or "").strip()
        if not question:
            defects.append(ContractDefect("step_question_missing", f"{location}.question", "task", "missing", True))
            continue
        outputs = [str(item) for item in raw.get("outputs") or [] if str(item).strip()]
        inputs = [str(item) for item in raw.get("inputs") or [] if str(item).strip()]
        if not outputs:
            defects.append(ContractDefect("step_output_missing", f"{location}.outputs", "output contract", "missing", True))
        try:
            steps.append(
                ProcedureStep(
                    id=step_id,
                    role=_role_for(raw, index, len(raw_steps)),
                    question=question,
                    inputs=inputs or ["request"],
                    outputs=outputs or [step_id],
                    checks=[str(item) for item in raw.get("checks") or ["structure"]],
                    failure_returns_to=_text_or_none(raw.get("failure_returns_to")),
                    budget=StepBudget(),
                    version=SCHEMA_VERSION,
                )
            )
        except ValidationError as exc:
            defects.append(ContractDefect("step_contract_invalid", location, "valid step", exc.__class__.__name__, True))

    requirements = _structured_requirements(payload.get("requirements"))
    defects.extend(_link_defects(steps, {item["id"] for item in requirements}))
    defects.extend(_requirement_defects(requirements, steps))
    if defects:
        return None, defects
    return Procedure(
        id=procedure_id,
        version=str(payload.get("schema_version") or SCHEMA_VERSION),
        revision=int(payload.get("revision") or 1),
        title=str(payload.get("title") or procedure_id),
        purpose=purpose,
        requirements=[str(item) for item in payload.get("requirements") or [] if str(item).strip()],
        steps=steps,
        dynamic=True,
        reusable=False,
    ), []


def _convert_known_legacy(
    payload: dict[str, Any],
    record: NormalizationRecord,
) -> tuple[dict[str, Any], list[ContractDefect]]:
    """Convert only the reviewed 3.0 shape. Unknown versions stay defects."""

    converted = dict(payload)
    version = str(converted.get("schema_version") or converted.get("version") or "").strip()
    if version and version not in {SCHEMA_VERSION, *_LEGACY_VERSIONS}:
        return converted, [
            ContractDefect(
                "unknown_schema_version",
                "schema_version",
                SCHEMA_VERSION,
                version,
                False,
            )
        ]
    if version in _LEGACY_VERSIONS:
        record.converted_from = version
        legacy_steps = converted.get("stages")
        if "steps" not in converted and isinstance(legacy_steps, list):
            converted["steps"] = legacy_steps
            record.filled.append("steps")
        if "purpose" not in converted and isinstance(converted.get("goal"), str):
            converted["purpose"] = converted["goal"]
            record.filled.append("purpose")
    return converted, []


def _requirement_defects(requirements: list[dict[str, str]], steps: list[ProcedureStep]) -> list[ContractDefect]:
    """Check links for requirements already structured by an earlier stage.

    This does not reinterpret the user request or require its wording to be
    copied into a step. An identifier alone is not coverage: the requirement
    must reach a judgment step and a later selection step must consume it.
    """

    if not requirements:
        return []
    defects: list[ContractDefect] = []
    for item in requirements:
        requirement_id = str(item["id"])
        handlers = [step for step in steps if requirement_id in step.inputs]
        judgments = [step for step in handlers if _is_judgment_step(step)]
        if not judgments:
            defects.append(
                ContractDefect(
                    "requirement_unlinked",
                    f"requirements.{requirement_id}",
                    "judgment step input",
                    "no judgment step consumes the requirement",
                    True,
                )
            )
            continue
        judgment_ids = {step.id for step in judgments}
        if not any(set(step.inputs) & judgment_ids and _is_selection_step(step) for step in steps):
            defects.append(
                ContractDefect(
                    "requirement_unjudged",
                    f"requirements.{requirement_id}",
                    "selection step consuming the judgment",
                    "judgment is not used by a selection step",
                    True,
                )
            )
    return defects


def _structured_requirements(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    requirements: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            requirements.append({"id": item.strip(), "statement": ""})
            seen.add(item.strip())
        elif isinstance(item, dict):
            requirement_id = str(item.get("id") or "").strip()
            if requirement_id and requirement_id not in seen:
                requirements.append(
                    {"id": requirement_id, "statement": str(item.get("statement") or "").strip()}
                )
                seen.add(requirement_id)
    return requirements


def _is_judgment_step(step: ProcedureStep) -> bool:
    markers = ("판정", "충족", "불충족", "미확인", "judgment", "met", "unmet")
    text = f"{step.role} {step.question} {' '.join(step.outputs)} {' '.join(step.checks)}"
    return any(marker in text for marker in markers)


def _is_selection_step(step: ProcedureStep) -> bool:
    markers = ("선정", "선택", "choose", "select")
    text = f"{step.role} {step.question} {' '.join(step.outputs)}"
    return any(marker in text for marker in markers)


def _extract_known(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    if isinstance(payload.get("steps"), list):
        return payload, ""
    found: list[tuple[str, dict[str, Any]]] = []
    for key in _KNOWN_WRAPPERS:
        value = payload.get(key)
        if isinstance(value, dict) and isinstance(value.get("steps"), list):
            found.append((key, value))
    if len(found) == 1:
        return found[0][1], found[0][0]
    if len(found) > 1:
        return None, ",".join(item[0] for item in found)
    return payload, ""


def _normalize_steps(raw_steps: list[Any]) -> tuple[list[dict[str, Any]], list[ContractDefect]]:
    steps: list[dict[str, Any]] = []
    defects: list[ContractDefect] = []
    assigned: dict[int, str] = {}
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            defects.append(ContractDefect("step_invalid", f"steps[{index}]", "object", type(raw).__name__, True))
            continue
        step = dict(raw)
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            step_id = f"D{index + 1}"
            step["id"] = step_id
            assigned[index] = step_id
        if not step.get("outputs"):
            role = _role_for(step, index, len(raw_steps))
            step["outputs"] = [_ROLE_OUTPUTS.get(role, step_id)]
        if not step.get("inputs"):
            step["inputs"] = ["request"] if index == 0 else [str(raw_steps[index - 1].get("id") or f"D{index}")]
        steps.append(step)
    if defects:
        return [], defects
    known = {str(step.get("id")) for step in steps}
    for index, step_id in assigned.items():
        for other in steps:
            refs = other.get("inputs")
            if isinstance(refs, list):
                other["inputs"] = [step_id if item == f"steps[{index}]" else item for item in refs]
            if other.get("failure_returns_to") == f"steps[{index}]":
                other["failure_returns_to"] = step_id
        if step_id not in known:
            defects.append(ContractDefect("step_id_unresolved", f"steps[{index}].id", "stable id", "unresolved", True))
    return steps, defects


def _link_defects(steps: list[ProcedureStep], requirement_ids: set[str] | None = None) -> list[ContractDefect]:
    defects: list[ContractDefect] = []
    known = {step.id for step in steps}
    accepted = known | {"request"} | (requirement_ids or set())
    graph = {step.id: [item for item in step.inputs if item in known] for step in steps}
    for step in steps:
        for ref in step.inputs:
            if ref not in accepted:
                defects.append(
                    ContractDefect("missing_reference", f"{step.id}.inputs", "existing step or request", ref, True)
                )
        if step.failure_returns_to and step.failure_returns_to not in known:
            defects.append(
                ContractDefect(
                    "missing_reference",
                    f"{step.id}.failure_returns_to",
                    "existing step",
                    step.failure_returns_to,
                    True,
                )
            )
    if _has_unbounded_cycle(graph):
        defects.append(ContractDefect("dependency_cycle", "steps", "acyclic dependencies", "cycle", True))
    produced = [item for step in steps for item in step.outputs]
    if "answer" not in produced and not any(step.role == "answer" for step in steps):
        defects.append(ContractDefect("answer_step_missing", "steps", "answer output", "missing", True))
    return defects


def _has_unbounded_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(child) for child in graph.get(node, [])):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _role_for(raw: dict[str, Any], index: int, count: int) -> str:
    role = str(raw.get("role") or "").strip()
    if role:
        return role
    outputs = {str(item) for item in raw.get("outputs") or []}
    for name, output in _ROLE_OUTPUTS.items():
        if name in outputs or output in outputs:
            return name
    if count == len(_CANONICAL_ROLES):
        return _CANONICAL_ROLES[index]
    if index == count - 1:
        return "answer"
    return "work"


def _default_title(request: str, procedure_id: str) -> str:
    compact = " ".join(request.split())
    return compact[:40] or procedure_id


def _purpose_fits(request: str, purpose: str) -> bool:
    request_terms = set(_TERM_RE.findall(request.lower()))
    purpose_terms = set(_TERM_RE.findall(purpose.lower()))
    if not request_terms or not purpose_terms:
        return False
    return bool(request_terms & purpose_terms)


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

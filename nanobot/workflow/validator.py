"""Workflow definition validation for the stage-1 contract."""
from __future__ import annotations

from dataclasses import dataclass, field

from nanobot.workflow.schema import WorkflowDefinition

_TERMINAL_TYPES = {"end"}
_VALID_STEP_TYPES = {"llm", "tool", "branch", "wait_user", "end"}
_FORBIDDEN_TOOL_NAMES = {"workflow", "spawn", "delegate"}


@dataclass(slots=True)
class WorkflowValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _tool_reference_required(definition: WorkflowDefinition, step_id: str) -> bool:
    """Return whether a tool step is required by at least one branch reference.

    Optional branches are pruned at runtime, so their missing tools should not
    reject the whole definition before execution.
    """

    required_seen = False
    referenced = False
    for step in definition.steps:
        branches = step.config.get("branches")
        if not isinstance(branches, dict):
            continue
        for branch in branches.values():
            if isinstance(branch, dict) and branch.get("next") == step_id:
                referenced = True
                required_seen = required_seen or bool(branch.get("required", True))
    return required_seen if referenced else True


def validate_definition(definition: WorkflowDefinition, *, available_tools: set[str] | None = None) -> WorkflowValidationResult:
    """Validate static workflow definition references.

    This is deliberately conservative and does not try to prove dynamic loop
    termination; runtime budgets handle that in later stages.
    """

    errors: list[str] = []
    step_ids = {step.id for step in definition.steps}
    if definition.start not in step_ids:
        errors.append(f"start step does not exist: {definition.start}")
    for step in definition.steps:
        if step.type not in _VALID_STEP_TYPES:
            errors.append(f"{step.id}: unsupported step type {step.type}")
        if step.next and step.next not in step_ids:
            errors.append(f"{step.id}: next step does not exist: {step.next}")
        if step.on_error and step.on_error not in step_ids:
            errors.append(f"{step.id}: on_error step does not exist: {step.on_error}")
        if step.type not in _TERMINAL_TYPES and not step.next and not step.config.get("branches"):
            errors.append(f"{step.id}: non-terminal step must define next or branches")
        if step.type == "tool":
            tool_name = str(step.config.get("tool") or "")
            if not tool_name:
                errors.append(f"{step.id}: tool step requires config.tool")
            if tool_name in _FORBIDDEN_TOOL_NAMES:
                errors.append(f"{step.id}: workflow may not call tool {tool_name!r}")
            if available_tools is not None and tool_name and tool_name not in available_tools:
                if _tool_reference_required(definition, step.id):
                    errors.append(f"{step.id}: tool is not available: {tool_name}")
    return WorkflowValidationResult(ok=not errors, errors=errors)

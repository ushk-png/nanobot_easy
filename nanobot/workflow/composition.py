"""Dynamic workflow composition for stage 4.

The static JSON definition is the durable template.  The composer produces a
per-run definition from that template by removing optional unavailable branches
and annotating prompts with the concrete action/tool set visible to this turn.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from nanobot.workflow.schema import WorkflowDefinition, WorkflowStep
from nanobot.workflow.validator import validate_definition


@dataclass(slots=True)
class WorkflowCompositionReport:
    definition_id: str
    selected_tools: list[str] = field(default_factory=list)
    removed_tools: list[str] = field(default_factory=list)
    removed_branches: list[str] = field(default_factory=list)
    available_actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return {
            "definition_id": self.definition_id,
            "selected_tools": list(self.selected_tools),
            "removed_tools": list(self.removed_tools),
            "removed_branches": list(self.removed_branches),
            "available_actions": list(self.available_actions),
            "notes": list(self.notes),
        }


def compose_workflow_definition(
    base: WorkflowDefinition,
    *,
    available_tools: set[str],
    request_text: str = "",
) -> tuple[WorkflowDefinition, WorkflowCompositionReport]:
    """Return a per-run workflow definition and composition report.

    Only optional branches are removed.  Required missing tool references remain
    validation errors so the caller gets a clear NEEDS_ATTENTION envelope.
    """

    definition = base.model_copy(deep=True)
    report = WorkflowCompositionReport(definition_id=definition.id)
    referenced_by_remaining_branch: set[str] = set()
    removed_step_ids: set[str] = set()

    for step in definition.steps:
        if step.type != "branch":
            continue
        branches = step.config.get("branches")
        if not isinstance(branches, dict):
            continue
        new_branches: dict[str, Any] = {}
        for action, branch in branches.items():
            if not isinstance(branch, dict):
                new_branches[str(action)] = branch
                report.available_actions.append(str(action))
                continue
            tool_name = branch.get("tool")
            next_step = branch.get("next")
            required = bool(branch.get("required", True))
            if tool_name and str(tool_name) not in available_tools and not required:
                report.removed_branches.append(str(action))
                report.removed_tools.append(str(tool_name))
                if isinstance(next_step, str):
                    removed_step_ids.add(next_step)
                continue
            new_branches[str(action)] = branch
            report.available_actions.append(str(action))
            if isinstance(next_step, str):
                referenced_by_remaining_branch.add(next_step)
            if tool_name and str(tool_name) in available_tools:
                report.selected_tools.append(str(tool_name))
        step.config["branches"] = new_branches

    removable = {
        step.id
        for step in definition.steps
        if step.id in removed_step_ids and step.id not in referenced_by_remaining_branch and step.type == "tool"
    }
    if removable:
        definition.steps = [step for step in definition.steps if step.id not in removable]

    selected = sorted(set(report.selected_tools))
    removed = sorted(set(report.removed_tools))
    actions = sorted(set(report.available_actions))
    report.selected_tools = selected
    report.removed_tools = removed
    report.available_actions = actions
    definition.referenced_tools = selected

    _annotate_prompts(definition, report=report, request_text=request_text)
    validation = validate_definition(definition, available_tools=available_tools)
    if not validation.ok:
        report.notes.extend(validation.errors)
    return definition, report


def _annotate_prompts(
    definition: WorkflowDefinition,
    *,
    report: WorkflowCompositionReport,
    request_text: str,
) -> None:
    suffix = (
        "\n\nDynamic workflow composition for this run:\n"
        f"- available_actions: {report.available_actions}\n"
        f"- selected_tools: {report.selected_tools}\n"
        f"- removed_optional_tools: {report.removed_tools}\n"
    )
    if request_text:
        suffix += f"- request_preview: {request_text[:500]}\n"
    for step in definition.steps:
        if step.type != "llm":
            continue
        prompt = str(step.config.get("prompt") or step.id)
        if "Dynamic workflow composition for this run:" not in prompt:
            step.config["prompt"] = prompt + suffix

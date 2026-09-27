"""Build workflow execution context snapshots.

The snapshot is intentionally plain JSON-compatible data so it can be stored
with a task before asynchronous execution starts.
"""
from __future__ import annotations

from typing import Any

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.session.conversation_focus import focus_runtime_lines
from nanobot.session.goal_state import goal_state_runtime_lines
from nanobot.workflow.schema import WorkflowPrincipal


def build_workflow_context_snapshot(
    *,
    user_text: str,
    principal: WorkflowPrincipal,
    session_metadata: dict[str, Any] | None = None,
    recent_history: list[dict[str, Any]] | None = None,
    tools: ToolRegistry | None = None,
    remaining_iterations: int | None = None,
    same_session_workflows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the stage-1 workflow context snapshot contract."""

    metadata = session_metadata or {}
    available_tools = sorted(tools.tool_names) if tools is not None else []
    return {
        "user_text": user_text,
        "principal": principal.model_dump(),
        "recent_history": list(recent_history or []),
        "goal_lines": goal_state_runtime_lines(metadata),
        "focus_lines": focus_runtime_lines(metadata),
        "available_tools": available_tools,
        "remaining_iterations": remaining_iterations,
        "same_session_workflows": list(same_session_workflows or []),
    }

"""Workflow principal construction and access checks."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.workflow.schema import WorkflowPrincipal, WorkflowTaskRecord


class WorkflowAccessDenied(PermissionError):
    """Raised when a caller cannot access a workflow task.

    Callers should return a not-found-like response and avoid disclosing whether
    a denied task exists.
    """


def build_principal(ctx: ToolContext) -> WorkflowPrincipal:
    """Build a trusted workflow principal from runtime context.

    LLM tool arguments must never provide workspace/session/channel identity.
    """

    req = current_request_context()
    workspace = str(Path(ctx.workspace).expanduser().resolve())
    if req is None:
        return WorkflowPrincipal(
            workspace=workspace,
            session_key="",
            channel="",
            chat_id="",
            subagent_depth=int(getattr(ctx, "subagent_depth", 0) or 0),
        )
    return WorkflowPrincipal(
        workspace=workspace,
        session_key=req.session_key or "",
        channel=req.channel,
        chat_id=req.chat_id,
        message_id=req.message_id,
        subagent_depth=int(getattr(ctx, "subagent_depth", 0) or 0),
    )


def same_principal(left: WorkflowPrincipal, right: WorkflowPrincipal) -> bool:
    return left.workspace == right.workspace and left.session_key == right.session_key


def ensure_task_access(principal: WorkflowPrincipal, task: WorkflowTaskRecord | dict[str, Any]) -> None:
    task_principal = task.principal if isinstance(task, WorkflowTaskRecord) else task.get("principal")
    if isinstance(task_principal, dict):
        task_principal = WorkflowPrincipal.model_validate(task_principal)
    if not isinstance(task_principal, WorkflowPrincipal) or not same_principal(principal, task_principal):
        raise WorkflowAccessDenied("workflow task is not available")

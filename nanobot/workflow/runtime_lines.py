"""Runtime-context lines for workflow waiting tasks."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.workflow.access import build_principal
from nanobot.workflow.schema import WorkflowPrincipal
from nanobot.workflow.store import WorkflowStore


def workflow_runtime_lines(state: Any, msg: Any, workspace: Path, *, skip: bool = False) -> list[str]:
    if skip:
        return []
    service = getattr(state, "workflow_service", None)
    if service is None:
        return []
    try:
        session_key = getattr(msg, "session_key", None) or f"{msg.channel}:{msg.chat_id}"
        principal = WorkflowPrincipal(
            workspace=str(Path(workspace).expanduser().resolve()),
            session_key=session_key,
            channel=str(getattr(msg, "channel", "")),
            chat_id=str(getattr(msg, "chat_id", "")),
            message_id=(getattr(msg, "metadata", {}) or {}).get("message_id"),
            subagent_depth=0,
        )
        waiting = service.waiting_tasks_for_principal(principal)
    except Exception:
        return []
    lines: list[str] = []
    for task in waiting[:5]:
        envelope = task.get("envelope") or {}
        question = str(envelope.get("question") or "").replace("\n", " ")[:300]
        lines.append(
            f'workflow waiting: task_id={task.get("task_id")}, '
            f'question_id={task.get("question_id")}, question="{question}", asked_at={task.get("updated_at")} '
        )
    return lines

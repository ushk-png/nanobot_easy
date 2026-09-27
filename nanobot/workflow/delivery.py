"""Workflow delivery hook and turn-local delivery state."""
from __future__ import annotations

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext


@dataclass(slots=True)
class WorkflowTurnDelivery:
    task_id: str
    content: str
    kind: str = "final"


_TURN_DELIVERY: ContextVar[WorkflowTurnDelivery | None] = ContextVar(
    "nanobot_workflow_turn_delivery",
    default=None,
)


def set_workflow_turn_delivery(task_id: str, content: str, *, kind: str = "final") -> None:
    _TURN_DELIVERY.set(WorkflowTurnDelivery(task_id=task_id, content=content, kind=kind))


def get_workflow_turn_delivery() -> WorkflowTurnDelivery | None:
    return _TURN_DELIVERY.get()


def clear_workflow_turn_delivery() -> None:
    _TURN_DELIVERY.set(None)


class WorkflowDeliveryHook(AgentHook):
    """Replace the final assistant content with the verified workflow payload."""

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        delivery = get_workflow_turn_delivery()
        if delivery is None:
            return content
        original = content or ""
        if original != delivery.content:
            digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:16]
            context.tool_events.append({
                "tool": "workflow",
                "event": "final_content_replaced",
                "hash": digest,
                "task_id": delivery.task_id,
            })
        return delivery.content

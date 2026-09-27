"""Runtime-control helpers for workflow resume routing."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.context import ToolContext, bind_request_context, reset_request_context, RequestContext
from nanobot.bus.events import OutboundMessage
from nanobot.workflow.access import build_principal


async def handle_workflow_runtime_control(state: Any, msg: Any, tools: Any) -> bool:
    metadata = getattr(msg, "metadata", {}) or {}
    workflow_meta = metadata.get("workflow") if isinstance(metadata, dict) else None
    task_id = None
    question_id = None
    if isinstance(workflow_meta, dict):
        task_id = workflow_meta.get("task_id")
        question_id = workflow_meta.get("question_id")
    task_id = task_id or metadata.get("workflow_task_id")
    question_id = question_id or metadata.get("workflow_question_id")
    if not task_id:
        return False
    service = getattr(state, "workflow_service", None)
    if service is None:
        return False
    session_key = getattr(msg, "session_key", None) or f"{msg.channel}:{msg.chat_id}"
    req = RequestContext(
        channel=msg.channel,
        chat_id=msg.chat_id,
        message_id=metadata.get("message_id"),
        session_key=session_key,
        metadata=dict(metadata),
    )
    token = bind_request_context(req)
    try:
        ctx = ToolContext(
            config=state.tools_config,
            workspace=str(Path(state.workspace).expanduser().resolve()),
            bus=state.bus,
            sessions=state.sessions,
            provider_snapshot_loader=lambda: state.provider,
            workflow_service=service,
            workflow_registry=tools,
        )
        principal = build_principal(ctx)
        envelope = await service.resume(
            principal=principal,
            task_id=str(task_id),
            question_id=str(question_id) if question_id else None,
            answer=msg.content or "",
            registry=tools,
            answer_source="runtime_control_message",
            input_reference="",
        )
    finally:
        reset_request_context(token)
    content = envelope.reason or envelope.question or json.dumps(envelope.model_dump(), ensure_ascii=False)
    await state.bus.publish_outbound(OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content=content,
        metadata={"workflow_task_id": task_id},
    ))
    return True

"""Workflow tool entry point."""
from __future__ import annotations

import json
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.workflow.access import build_principal
from nanobot.workflow.config import WorkflowToolConfig
from nanobot.workflow.schema import WorkflowAction, WorkflowEnvelope


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Workflow action to perform.",
            enum=[action.value for action in WorkflowAction],
        ),
        input=StringSchema(
            "User request or resume response for run/resume actions.",
            nullable=True,
        ),
        task_id=StringSchema(
            "Workflow task id for resume/status/cancel.",
            nullable=True,
        ),
        question_id=StringSchema(
            "Workflow question id for resume.",
            nullable=True,
        ),
        workflow_id=StringSchema(
            "Workflow definition id to run.",
            nullable=True,
        ),
        reason=StringSchema(
            "Cancellation reason.",
            nullable=True,
        ),
        required=["action"],
    )
)
class WorkflowTool(Tool):
    config_key = "workflow"
    _scopes = {"core", "subagent"}

    def __init__(self, ctx: ToolContext, config: WorkflowToolConfig):
        self._ctx = ctx
        self._config = config

    @classmethod
    def config_cls(cls) -> type[WorkflowToolConfig]:
        return WorkflowToolConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        cfg = getattr(getattr(ctx, "config", None), "workflow", None)
        return bool(getattr(cfg, "enabled", False))

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        raw = getattr(getattr(ctx, "config", None), "workflow", None)
        cfg = raw if isinstance(raw, WorkflowToolConfig) else WorkflowToolConfig.model_validate(raw or {})
        return cls(ctx, cfg)

    @property
    def name(self) -> str:
        return "workflow"

    @property
    def description(self) -> str:
        return (
            "Run, resume, inspect, cancel, or list durable workflow tasks. "
            "Use when a task needs stateful judgment, conditional branching, repeated validation, "
            "or user waiting. Can be selected as a method for a composite-task subtask. "
            "The tool is disabled by default until config.tools.workflow.enabled=true."
        )

    async def execute(
        self,
        action: str,
        input: str | None = None,
        task_id: str | None = None,
        question_id: str | None = None,
        workflow_id: str | None = None,
        reason: str | None = None,
        **_kwargs: Any,
    ) -> str:
        if not self._config.enabled:
            envelope = WorkflowEnvelope.disabled()
            return json.dumps(envelope.model_dump(), ensure_ascii=False, indent=2)

        service = getattr(self._ctx, "workflow_service", None)
        if service is None:
            envelope = WorkflowEnvelope(
                state="NEEDS_ATTENTION",
                reason="workflow service is not available in this entry point",
                next_hint="Run through AgentLoop/gateway or attach WorkflowService to ToolContext.",
            )
            return json.dumps(envelope.model_dump(), ensure_ascii=False, indent=2)

        principal = build_principal(self._ctx)
        request_ctx = current_request_context()
        registry = getattr(self._ctx, "workflow_registry", None)
        normalized = str(action)
        if normalized == WorkflowAction.LIST.value:
            return json.dumps(service.list_definitions(), ensure_ascii=False, indent=2)
        if normalized == WorkflowAction.STATUS.value:
            result = service.status(principal=principal, task_id=task_id)
            payload = [row for row in result] if isinstance(result, list) else result.model_dump()
            return json.dumps(payload, ensure_ascii=False, indent=2)
        if normalized == WorkflowAction.CANCEL.value:
            if not task_id:
                envelope = WorkflowEnvelope(state="NEEDS_ATTENTION", reason="cancel requires task_id")
            else:
                envelope = service.cancel(principal=principal, task_id=task_id, reason=reason)
            return json.dumps(envelope.model_dump(), ensure_ascii=False, indent=2)
        if normalized == WorkflowAction.RESUME.value:
            if not task_id:
                envelope = WorkflowEnvelope(state="NEEDS_ATTENTION", reason="resume requires task_id")
            else:
                if registry is None:
                    from nanobot.agent.tools.registry import ToolRegistry
                    registry = ToolRegistry()
                envelope = await service.resume(
                    principal=principal,
                    task_id=task_id,
                    question_id=question_id,
                    answer=input or "",
                    registry=registry,
                )
            return json.dumps(envelope.model_dump(), ensure_ascii=False, indent=2)
        if normalized == WorkflowAction.RUN.value:
            if registry is None:
                from nanobot.agent.tools.registry import ToolRegistry
                registry = ToolRegistry()
            workflow_context = self._system_workflow_context(request_ctx, fallback_user_text=input or "")
            envelope = await service.run(
                principal=principal,
                user_text=workflow_context["user_text"],
                registry=registry,
                workflow_id=workflow_id or "situation_judgment.v1",
                session_metadata=workflow_context["session_metadata"],
                recent_history=workflow_context["recent_history"],
                goal=input or "",
            )
            return json.dumps(envelope.model_dump(), ensure_ascii=False, indent=2)

        envelope = WorkflowEnvelope(state="NEEDS_ATTENTION", reason=f"unknown workflow action: {action}")
        return json.dumps(envelope.model_dump(), ensure_ascii=False, indent=2)

    def _system_workflow_context(self, request_ctx: Any | None, *, fallback_user_text: str) -> dict[str, Any]:
        session = None
        session_key = getattr(request_ctx, "session_key", None) if request_ctx is not None else None
        if session_key and getattr(self._ctx, "sessions", None) is not None:
            try:
                session = self._ctx.sessions.get_or_create(str(session_key))
            except Exception:
                session = None
        metadata = dict(getattr(session, "metadata", {}) or {}) if session is not None else {}
        recent_history: list[dict[str, Any]] = []
        if session is not None:
            try:
                recent_history = list(session.get_history(max_messages=20))
            except Exception:
                recent_history = []
        user_text = self._user_text_from_session(session, getattr(request_ctx, "message_id", None) if request_ctx is not None else None)
        if not user_text:
            user_text = fallback_user_text
        return {
            "user_text": user_text,
            "recent_history": recent_history,
            "session_metadata": metadata,
        }

    @staticmethod
    def _user_text_from_session(session: Any | None, message_id: str | None) -> str:
        if session is None:
            return ""
        messages = list(getattr(session, "messages", []) or [])
        if message_id:
            for message in reversed(messages):
                if message.get("role") == "user" and str(message.get("message_id") or "") == str(message_id):
                    content = message.get("content")
                    return content if isinstance(content, str) else ""
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else ""
        return ""

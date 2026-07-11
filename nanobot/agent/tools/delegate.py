"""Delegate tool for synchronous subagent execution."""

from __future__ import annotations

from contextvars import ContextVar
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import NumberSchema, StringSchema, tool_parameters_schema
from nanobot.security.workspace_access import current_workspace_scope
from nanobot.skill_store import SkillStore

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager

_DEPENDENT_CONTEXT_MARKERS = (
    "prior wave",
    "previous wave",
    "previous output",
    "based on the summary",
    "based on prior",
    "use the summary",
    "선행",
    "이전 웨이브",
    "앞선 결과",
    "요약 결과",
    "요약을 바탕",
    "바탕으로",
)


@tool_parameters(
    tool_parameters_schema(
        profile=StringSchema(
            "Subagent profile to use. MUST be one of the profiles listed in the "
            "tool description. Choose based on each profile's when_to_use / "
            "when_not_to_use criteria."
        ),
        task=StringSchema(
            "The self-contained task for the subagent to complete. Include paths, "
            "constraints, and concrete references; do not rely on conversation context."
        ),
        expected_output=StringSchema(
            "What the subagent must return: required format, content, and acceptance criteria."
        ),
        context=StringSchema(
            "Optional explicit context needed for the task, such as prior findings, "
            "requirements, file paths, or decisions.",
            nullable=True,
        ),
        temperature=NumberSchema(
            description=(
                "Optional sampling temperature for the subagent "
                "(0.0 = deterministic, higher = more creative). "
                "Defaults to the profile or provider configuration."
            ),
            minimum=0.0,
            maximum=2.0,
        ),
        required=["profile", "task", "expected_output"],
    )
)
class DelegateTool(Tool, ContextAware):
    """Tool to run a specialized subagent synchronously."""

    def __init__(self, manager: "SubagentManager", workspace: str | None = None):
        self._manager = manager
        self._workspace = workspace
        self._origin_channel: ContextVar[str] = ContextVar("delegate_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("delegate_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("delegate_session_key", default="cli:direct")

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager, workspace=ctx.workspace)

    def set_context(self, ctx: RequestContext) -> None:
        """Set the origin context for subagent execution."""
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        lines = [
            "Run a specialized subagent synchronously and return its final result immediately.",
            "Use this when the current task depends on the subagent result before continuing.",
            "For long-running or parallel independent work, use spawn instead.",
            "Pick the profile whose when_to_use best matches the task. "
            "Never pick a profile whose when_not_to_use matches the task.",
            "The task must be self-contained: avoid pronouns and include relevant "
            "paths, URLs, constraints, and context because the subagent cannot see "
            "the current conversation unless you pass it in context.",
            "",
            "Available profiles:",
        ]
        profiles = getattr(self._manager, "profiles", {})
        for name, profile in profiles.items():
            lines.append(f"- {name}: {profile.description}")
            if profile.when_to_use:
                lines.append(f"    when_to_use: {'; '.join(profile.when_to_use)}")
            if profile.when_not_to_use:
                lines.append(f"    when_NOT_to_use: {'; '.join(profile.when_not_to_use)}")
        if not profiles:
            lines.append("- general: general-purpose subagent (no profiles configured)")
        return "\n".join(lines)

    async def execute(
        self,
        profile: str = "",
        task: str = "",
        expected_output: str = "",
        context: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> str:
        """Delegate a task and wait for the result."""
        started = time.perf_counter()

        def record(result: str, *, gate_result: str | None = None) -> None:
            if not self._workspace:
                return
            SkillStore(self._workspace).record_trace(
                trace_id=f"delegate:{uuid4().hex}",
                session_key=self._session_key.get(),
                selected_skill=None,
                selection_reason="delegate",
                executed_by=profile or None,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                gate_result=gate_result,
                notes=(task or result or "")[:800],
            )

        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            result = (
                f"Cannot delegate subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before delegating a new one."
            )
            record(result, gate_result="error")
            return result
        profiles = getattr(self._manager, "profiles", {})
        if profiles and profile not in profiles:
            valid = ", ".join(profiles)
            result = (
                f"Error: unknown profile '{profile}'. "
                f"Choose one of: {valid}. Re-read the profile cards and retry."
            )
            record(result, gate_result="error")
            return result
        combined = f"{task}\n{expected_output}".lower()
        if not (context or "").strip() and any(marker in combined for marker in _DEPENDENT_CONTEXT_MARKERS):
            result = (
                "Error: dependent delegate task appears to require prior wave output, "
                "but context is empty. Build a self-contained context package with the "
                "prior wave result before delegating."
            )
            record(result, gate_result="error")
            return result
        result = await self._manager.delegate(
            task=task,
            profile=profile,
            expected_output=expected_output,
            context=context or "",
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            temperature=temperature,
            workspace_scope=current_workspace_scope(),
        )
        record(str(result), gate_result="ok")
        return result

"""Spawn tool for creating background subagents."""

from __future__ import annotations

import inspect
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import NumberSchema, StringSchema, tool_parameters_schema
from nanobot.security.workspace_access import current_workspace_scope
from nanobot.skill_store import SkillStore

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        profile=StringSchema(
            "Subagent profile to use. MUST be one of the profiles listed in the "
            "tool description. Choose based on each profile's when_to_use / "
            "when_not_to_use criteria."
        ),
        expected_output=StringSchema(
            "What the subagent must return when done: required format, content, "
            "and acceptance criteria. Be specific."
        ),
        label=StringSchema("Optional short label for the task (for display)"),
        temperature=NumberSchema(
            description=(
                "Optional sampling temperature for the subagent "
                "(0.0 = deterministic, higher = more creative). "
                "Defaults to the provider's configured temperature."
            ),
            minimum=0.0,
            maximum=2.0,
        ),
        required=["task", "profile", "expected_output"],
    )
)
class SpawnTool(Tool, ContextAware):
    """Tool to spawn a specialized subagent for background task execution."""

    _scopes = {"core", "subagent"}

    def __init__(self, manager: "SubagentManager", depth: int = 0, workspace: str | None = None):
        self._manager = manager
        self._depth = depth
        self._workspace = workspace
        self._origin_channel: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "spawn_origin_message_id",
            default=None,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager, depth=getattr(ctx, "subagent_depth", 0), workspace=ctx.workspace)

    def set_context(self, ctx: RequestContext) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        lines = [
            "Spawn a specialized subagent to handle a task in the background.",
            "Use this for complex or time-consuming tasks that can run independently.",
            "The subagent will complete the task and report back when done.",
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful.",
            "Pick the profile whose when_to_use best matches the task. "
            "Never pick a profile whose when_not_to_use matches the task.",
            "The task must be self-contained: avoid pronouns and include relevant "
            "paths, URLs, constraints, and context because the subagent cannot see "
            "the current conversation.",
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
        task: str,
        profile: str = "",
        expected_output: str = "",
        label: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        started = time.perf_counter()

        def record(result: str, *, gate_result: str | None = None) -> None:
            if not self._workspace:
                return
            SkillStore(self._workspace).record_trace(
                trace_id=f"spawn:{uuid4().hex}",
                session_key=self._session_key.get(),
                selected_skill=None,
                selection_reason="spawn",
                executed_by=profile or None,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                gate_result=gate_result,
                notes=(task or result or "")[:800],
            )

        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            result = (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before spawning a new one."
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
        spawn_kwargs: dict[str, Any] = {
            "task": task,
            "label": label,
            "origin_channel": self._origin_channel.get(),
            "origin_chat_id": self._origin_chat_id.get(),
            "session_key": self._session_key.get(),
            "origin_message_id": self._origin_message_id.get(),
            "temperature": temperature,
            "workspace_scope": current_workspace_scope(),
        }
        try:
            parameters = inspect.signature(self._manager.spawn).parameters
            accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        except (TypeError, ValueError):
            parameters = {}
            accepts_kwargs = True
        for name, value in {
            "profile": profile,
            "expected_output": expected_output,
            "depth": self._depth + 1,
        }.items():
            if accepts_kwargs or name in parameters:
                spawn_kwargs[name] = value
        result = await self._manager.spawn(**spawn_kwargs)
        record(str(result), gate_result="ok")
        return result

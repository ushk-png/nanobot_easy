"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, SubagentProfile, ToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.security.workspace_access import (
    WorkspaceScope,
    bind_workspace_scope,
    reset_workspace_scope,
    workspace_sandbox_status,
)
from nanobot.utils.prompt_templates import render_template


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SubagentRunOutcome:
    """Final result of a subagent run before delivery."""

    content: str
    status: str
    stop_reason: str | None = None


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        fail_on_tool_error: bool | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        profiles: dict[str, SubagentProfile] | None = None,
        max_depth: int = 2,
        student_mode_config: Any | None = None,
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self.fail_on_tool_error = (
            fail_on_tool_error
            if fail_on_tool_error is not None
            else defaults.fail_on_tool_error
        )
        self.profiles = profiles or {}
        self.max_depth = max_depth
        self.student_mode_config = student_mode_config
        self.runner = AgentRunner(provider)
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            file=self.tools_config.file,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        profile: SubagentProfile | None = None,
        depth: int = 1,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            subagent_manager=self,
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
            subagent_depth=depth,
            student_mode=self.student_mode_config,
        )
        ToolLoader().load(ctx, registry, scope="subagent")

        if profile is not None and profile.tools is not None:
            allowed = set(profile.tools) | {"spawn"}
            for name in list(registry.tool_names):
                if name not in allowed:
                    registry.unregister(name)

        allow_spawn = depth < self.max_depth and profile is not None and profile.can_spawn
        if not allow_spawn:
            registry.unregister("spawn")

        return registry

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        profile: str = "",
        expected_output: str = "",
        label: str | None = None,
        depth: int = 1,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        if depth > self.max_depth:
            return (
                f"Cannot spawn: max subagent depth ({self.max_depth}) reached. "
                "Complete this task directly instead of delegating."
            )
        if self.profiles and profile not in self.profiles:
            valid = ", ".join(self.profiles)
            return f"Error: unknown profile '{profile}'. Choose one of: {valid}."

        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                origin_message_id,
                temperature,
                workspace_scope,
                profile,
                expected_output,
                depth,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def delegate(
        self,
        task: str,
        profile: str = "",
        expected_output: str = "",
        context: str = "",
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
    ) -> str:
        """Run a subagent synchronously and return its result directly."""
        if self.profiles and profile not in self.profiles:
            valid = ", ".join(self.profiles)
            return f"Error: unknown profile '{profile}'. Choose one of: {valid}."

        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}
        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status
        logger.info("Delegated subagent [{}]: {}", task_id, display_label)
        try:
            outcome = await self._execute_subagent(
                task_id=task_id,
                task=task,
                label=display_label,
                origin=origin,
                status=status,
                temperature=temperature,
                workspace_scope=workspace_scope,
                profile_name=profile,
                expected_output=expected_output,
                depth=1,
                task_context=context,
            )
            if outcome.status == "ok":
                return outcome.content
            return f"Subagent failed: {outcome.content}"
        finally:
            self._task_statuses.pop(task_id, None)

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        profile_name: str = "",
        expected_output: str = "",
        depth: int = 1,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        outcome = await self._execute_subagent(
            task_id=task_id,
            task=task,
            label=label,
            origin=origin,
            status=status,
            temperature=temperature,
            workspace_scope=workspace_scope,
            profile_name=profile_name,
            expected_output=expected_output,
            depth=depth,
        )
        await self._announce_result(
            task_id,
            label,
            task,
            outcome.content,
            origin,
            outcome.status,
            origin_message_id,
        )

    async def _execute_subagent(
        self,
        *,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str | None],
        status: SubagentStatus,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        profile_name: str = "",
        expected_output: str = "",
        depth: int = 1,
        task_context: str = "",
    ) -> SubagentRunOutcome:
        """Execute the subagent task and return its raw outcome."""
        prof = self.profiles.get(profile_name)

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            root = workspace_scope.project_path if workspace_scope is not None else self.workspace
            cfg = None
            if workspace_scope is not None:
                cfg = self._subagent_tools_config()
                cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
            tools = self._build_tools(
                workspace=root,
                tools_config=cfg,
                profile=prof,
                depth=depth,
            )
            system_prompt = self._build_subagent_prompt(
                workspace=root,
                profile_name=profile_name,
                profile=prof,
                expected_output=expected_output,
                task_context=task_context,
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            sess_key = origin.get("session_key")
            llm_timeout = (
                self._llm_wall_timeout_for_session(sess_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            token = bind_workspace_scope(workspace_scope) if workspace_scope is not None else None
            try:
                result = await self.runner.run(AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=(prof.model if prof and prof.model else self.model),
                    temperature=(
                        temperature
                        if temperature is not None
                        else (prof.temperature if prof else None)
                    ),
                    max_iterations=(
                        prof.max_iterations
                        if prof and prof.max_iterations
                        else self.max_iterations
                    ),
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=_SubagentHook(task_id, status),
                    max_iterations_message="Task completed but no final response was generated.",
                    finalize_on_max_iterations=False,
                    error_message=None,
                    fail_on_tool_error=self.fail_on_tool_error,
                    checkpoint_callback=_on_checkpoint,
                    session_key=sess_key,
                    workspace=root,
                    llm_timeout_s=llm_timeout,
                ))
            finally:
                if token is not None:
                    reset_workspace_scope(token)
            status.phase = "done"
            status.stop_reason = result.stop_reason

            if result.stop_reason == "tool_error":
                status.tool_events = list(result.tool_events)
                return SubagentRunOutcome(
                    content=self._format_partial_progress(result),
                    status="error",
                    stop_reason=result.stop_reason,
                )
            if result.stop_reason == "error":
                return SubagentRunOutcome(
                    content=result.error or "Error: subagent execution failed.",
                    status="error",
                    stop_reason=result.stop_reason,
                )
            final_result = result.final_content or "Task completed but no final response was generated."
            if expected_output and len(final_result.strip()) < 20:
                logger.warning("Subagent [{}] result failed expected_output gate", task_id)
                return SubagentRunOutcome(
                    content=(
                        "Result did not satisfy the expected output.\n"
                        f"Expected: {expected_output}\n"
                        f"Got: {final_result}"
                    ),
                    status="error",
                    stop_reason=result.stop_reason,
                )
            logger.info("Subagent [{}] completed successfully", task_id)
            return SubagentRunOutcome(
                content=final_result,
                status="ok",
                stop_reason=result.stop_reason,
            )

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.exception("Subagent [{}] failed", task_id)
            return SubagentRunOutcome(content=f"Error: {e}", status="error", stop_reason="error")

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(
        self,
        workspace: Path | None = None,
        profile_name: str = "",
        profile: SubagentProfile | None = None,
        expected_output: str = "",
        task_context: str = "",
    ) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        root = workspace or self.workspace
        loader = SkillsLoader(
            root,
            disabled_skills=self.disabled_skills,
            include_system=False,
        )
        skills_summary = loader.build_skills_summary()
        preloaded_skills = ""
        if profile and profile.skills:
            preloaded_skills = loader.load_skills_for_context(profile.skills)
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
            profile_name=profile_name,
            profile_description=(profile.description if profile else ""),
            preloaded_skills=preloaded_skills or "",
            expected_output=expected_output,
            task_context=task_context,
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )

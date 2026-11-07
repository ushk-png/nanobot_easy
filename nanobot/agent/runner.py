"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.agent.answer_delivery import (
    ANSWER_BODY_CORRECTION,
    ANSWER_BODY_INCOMPLETE,
    ANSWER_BODY_NOTICE,
    needs_answer_body,
)
from nanobot.agent.context_governance import (
    ContextGovernanceConfig,
    ContextGovernor,
)
from nanobot.agent.context_selection import ContextCapacityError
from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.premature_final import CORRECTION, INCOMPLETE, is_premature_final
from nanobot.agent.repetition_guard import RepeatedSentenceGuard
from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.session.history_visibility import is_hidden_history_message
from nanobot.utils.file_edit_events import (
    StreamingFileEditTracker,
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)
from nanobot.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from nanobot.utils.helpers import (
    IncrementalThinkExtractor,
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    strip_reasoning_tags,
    strip_think,
)
from nanobot.utils.progress_events import (
    invoke_file_edit_progress,
    invoke_on_progress,
    on_progress_accepts_file_edit_events,
)
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_budget_exhausted_finalization_message,
    build_finalization_retry_message,
    build_goal_continue_message,
    build_length_recovery_message,
    is_blank_text,
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)

GoalContinueMessage = str | Callable[[], str | None]

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
# Backward-compatible module attribute for tests/extensions that monkeypatch
# the former single-file tracker hook. Runtime uses prepare_file_edit_trackers.
prepare_file_edit_tracker = _prepare_file_edit_tracker


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: GoalContinueMessage | None = None
    finalize_on_max_iterations: bool = True
    selected_context: Any | None = None
    selection_target_tokens: int = 16000
    premature_final_guard: bool = False
    premature_final_session_metadata: dict[str, Any] | None = None
    task_note_enabled: bool = False
    task_note_metadata: dict[str, Any] | None = None
    task_state: Any | None = None
    task_note_persist_callback: Any | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider
        self.context_governor = ContextGovernor()

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
                and not is_hidden_history_message(injection)
                and not is_hidden_history_message(messages[-1])
            ):
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
        allow_goal_continue: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        injections: list[dict[str, Any]] = []
        real_injection = False
        if injection_cycles < _MAX_INJECTION_CYCLES:
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if not injections and allow_goal_continue and assistant_message is not None:
            predicate = spec.goal_active_predicate
            if predicate is not None and predicate():
                injections = [self._build_goal_continue_message(spec)]
        if not injections:
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            logger.info(
                "Injected {} follow-up message(s) {} ({}/{})",
                len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
            )
        else:
            logger.info("Injected sustained-goal continuation {}", phase)
        return True, injection_cycles

    def _build_goal_continue_message(self, spec: AgentRunSpec) -> dict[str, str]:
        custom = spec.goal_continue_message
        if callable(custom):
            try:
                custom = custom()
            except Exception:
                logger.exception("goal_continue_message callback failed")
                custom = None
        return build_goal_continue_message(custom)

    async def _drain_injections(self, spec: AgentRunSpec, *, wait: bool = True) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            options = {"limit": _MAX_INJECTIONS_PER_TURN} if accepts_limit else {}
            if "wait" in signature.parameters:
                options["wait"] = wait
            items = await spec.injection_callback(**options)
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                if self._has_injection_content(item.get("content")):
                    injected_messages.append(item)
                continue
            if isinstance(item, dict):
                continue
            content = getattr(item, "content") if hasattr(item, "content") else str(item)
            if self._has_injection_content(content):
                injected_messages.append({"role": "user", "content": content})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        if spec.selected_context and injected_messages:
            if spec.selected_context.control:
                spec.selected_context.control.ingest_messages(injected_messages)
            spec.selected_context.completion_reports.clear()
            spec.selected_context.trace.count("redesigns", "new_user_instruction_executor_reconciliation")
        if spec.task_state is not None and injected_messages:
            spec.task_state.revise(injected_messages)
            injected_messages.append(spec.task_state.guidance())
        return injected_messages

    @staticmethod
    def _has_injection_content(content: Any) -> bool:
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(content)
        return True

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        from nanobot.agent.context_control import TaskControl
        from nanobot.agent.context_evidence import active_evidence_capture
        from nanobot.agent.context_trace import active_context_trace

        capture_token = active_evidence_capture.set(bool(spec.selected_context))
        trace_token = active_context_trace.set(spec.selected_context.trace if spec.selected_context else active_context_trace.get())
        if spec.selected_context:
            spec.selected_context.trace.stage = "legacy_fallback" if spec.selected_context.legacy_fallback else "execute"
            if spec.selected_context.control is None:
                spec.selected_context.control = TaskControl(spec.selected_context)
        from nanobot.agent.task_note import TaskNote, TaskTools, active_task_note, needs_note

        task_token = None
        if spec.task_note_enabled and needs_note(spec.initial_messages):
            note = TaskNote(spec.initial_messages, spec.workspace, spec.task_note_metadata,
                            spec.task_note_persist_callback)
            spec = replace(spec, task_state=note, tools=TaskTools(spec.tools, note),
                           initial_messages=[*spec.initial_messages, note.guidance()],
                           stream_progress_deltas=False)
            task_token = active_task_note.set(note)
        if spec.selected_context and getattr(spec.selected_context, "allocation", None):
            spec = replace(spec, stream_progress_deltas=False)
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        context = AgentRunHookContext(messages=deepcopy(messages))

        try:
            await hook.before_run(context)
            result = await self._run_core(spec, hook, messages)
        except asyncio.CancelledError as exc:
            if spec.task_state is not None:
                spec.task_state.persist("cancelled")
            if spec.selected_context and spec.selected_context.control:
                spec.selected_context.control.save(lambda state: state.update(
                    instruction_status="CANCELLED", status="CANCELLED"))
            context.messages = deepcopy(messages)
            context.stop_reason = "cancelled"
            context.error = None
            context.exception = exc
            raise
        except Exception as exc:
            if spec.task_state is not None:
                spec.task_state.persist("error")
            context.messages = deepcopy(messages)
            context.stop_reason = "error"
            context.error = f"Error: {type(exc).__name__}: {exc}"
            context.exception = exc
            await hook.on_error(context)
            raise
        else:
            if spec.selected_context and not spec.selected_context.legacy_fallback:
                result.final_content, result.stop_reason = spec.selected_context.verdict(result.final_content, result.stop_reason)
                if result.messages and result.messages[-1].get("role") == "assistant":
                    result.messages[-1]["content"] = result.final_content
            if spec.task_state is not None:
                spec.task_state.persist(result.stop_reason)
                spec.task_state.metadata["task_note"]["metrics"] = {
                    "usage": result.usage,
                    "elapsed_ms": int((time.monotonic() - spec.task_state.started) * 1000),
                    "tool_calls": len(spec.task_state.events), "cost": None,
                    "model_calls": spec.task_state.model_calls,
                }
            context.messages = deepcopy(result.messages)
            context.final_content = result.final_content
            context.tools_used = list(result.tools_used)
            context.usage = dict(result.usage)
            context.stop_reason = result.stop_reason
            context.error = result.error
            context.tool_events = deepcopy(result.tool_events)
            context.had_injections = result.had_injections
            context.exception = None
            if context.error is not None:
                await hook.on_error(context)
            await hook.after_run(context)
            return result
        finally:
            if spec.selected_context:
                spec.selected_context.trace.persist()
            active_context_trace.reset(trace_token)
            active_evidence_capture.reset(capture_token)
            if task_token is not None:
                active_task_note.reset(task_token)
            context.messages = deepcopy(messages)
            if context.exception is None:
                await hook.on_finally(context)
            else:
                try:
                    await hook.on_finally(context)
                except Exception:
                    logger.exception(
                        "AgentHook.on_finally error after {}",
                        context.stop_reason or "run exception",
                    )

    async def _run_core(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        had_injections = False
        injection_cycles = 0
        compacted_tool_call_ids: set[str] = set()
        premature_final_corrections = 0
        answer_body_corrections = 0
        answer_fix_corrections = 0
        task_corrections = 0
        report_corrections = 0

        async def guard_final(clean, assistant_message, iteration, *, already_appended=False):
            nonlocal premature_final_corrections, task_corrections, report_corrections, stop_reason
            nonlocal answer_body_corrections, answer_fix_corrections
            if spec.task_state is not None:
                if spec.task_state.update is not None:
                    spec.task_state.update.final_answer = hook.finalize_content(
                        context, spec.task_state.update.final_answer,
                    ) or ""
                defects, terminal = spec.task_state.final_check()
                if terminal:
                    stop_reason = "incomplete"
                    clean = str(clean or "") + "\n\n미완료 상태: " + spec.task_state.update.explanation
                    assistant_message["content"] = clean
                    return clean, False
                if defects:
                    reporting_only = all(d == "answer_required" or d.startswith("answer_correction:")
                                         for d in defects)
                    can_correct = report_corrections == 0 if reporting_only else task_corrections == 0
                    if can_correct and iteration + 1 < spec.max_iterations:
                        if reporting_only:
                            report_corrections += 1
                            spec.task_state.reporting_only = True
                        else:
                            task_corrections += 1
                        assistant_message["_hidden_history"] = True
                        if not already_appended:
                            messages.append(assistant_message)
                        feedback = (
                            "Completion defects (all): " + ", ".join(defects)
                            + ". Resolve these in one recovery process within the remaining budget. "
                            "Multiple reads/checks are allowed. Check unknown writes before retry; "
                            "never repeat successful/denied writes. If blocked, record partial, "
                            "blocked or clarification in update_task and report the actual limit. "
                            "Do not infer intent from response wording. "
                        )
                        if reporting_only:
                            feedback += (
                                "The requested result has not been delivered. Use the already "
                                "verified evidence to provide the actual findings/cause, evidence "
                                "and uncertainty in update_task.final_answer, not an announcement "
                                "that you will close/report later. Do not reread or re-execute "
                                "successful work just to produce the report. This is report "
                                "assembly within recovery, not a new execution stage. "
                                "Current request_id=" + spec.task_state.request_id
                            )
                        else:
                            feedback += spec.task_state.guidance()["content"]
                        messages.append({"role": "user", "_hidden_history": True, "content": feedback})
                        return clean, True
                    stop_reason = "incomplete"
                    clean = spec.task_state.incomplete(defects)
                    assistant_message["content"] = clean
                    return clean, False
                clean = spec.task_state.update.final_answer.strip()
                assistant_message["content"] = clean
            if spec.selected_context and not spec.selected_context.legacy_fallback:
                checked, checked_status = spec.selected_context.verdict(clean)
                if checked_status != "completed" and answer_fix_corrections == 0:
                    # t47/t49: at most one in-loop fix, routed by kind (answer content / evidence link);
                    # no fix call for an open item shown to decide the answer (t50: a target/permission
                    # condition holds changes only), a verifier defect, or when the iteration budget is
                    # used up. The fix is an ordinary iteration (counts in budget).
                    from nanobot.agent.context_plan import fix_route
                    _, fix = fix_route(spec.selected_context, clean, iteration + 1 < spec.max_iterations)
                    if fix:
                        answer_fix_corrections += 1
                        assistant_message["_hidden_history"] = True
                        if not already_appended:
                            messages.append(assistant_message)
                        messages.append({"role": "user", "_hidden_history": True, "content": fix})
                        return clean, True
                if checked_status != "completed":
                    stop_reason = checked_status
                    assistant_message["unverified_content"] = clean
                    assistant_message["content"] = checked
                    return checked, False
            if spec.premature_final_guard and needs_answer_body(clean, messages):
                if answer_body_corrections < 2 and iteration + 1 < spec.max_iterations:
                    answer_body_corrections += 1
                    if not already_appended:
                        messages.append(assistant_message)
                    await self._emit_checkpoint(spec, {
                        "phase": "answer_body_recovery", "iteration": iteration, "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [], "pending_tool_calls": [],
                    })
                    messages.append({"role": "user", "content": ANSWER_BODY_CORRECTION,
                                     "_hidden_history": True, "_answer_delivery_feedback": True})
                    logger.info("Answer body recovery: attempt={} remaining_iterations={}",
                                answer_body_corrections, spec.max_iterations - iteration - 1)
                    if spec.progress_callback is not None:
                        with suppress(Exception):
                            await invoke_on_progress(spec.progress_callback, ANSWER_BODY_NOTICE,
                                                     tool_hint=False)
                    return clean, True
                stop_reason = "incomplete"
                clean = (str(clean or "") + "\n\n" + ANSWER_BODY_INCOMPLETE
                         + f" (본문 복구 시도: {answer_body_corrections}회)")
                assistant_message["content"] = clean
                return clean, False
            if not spec.premature_final_guard or not is_premature_final(
                clean, messages, spec.tools, tool_events,
                spec.premature_final_session_metadata,
            ):
                return clean, False
            if premature_final_corrections == 0 and iteration + 1 < spec.max_iterations:
                premature_final_corrections += 1
                if not already_appended:
                    messages.append(assistant_message)
                await self._emit_checkpoint(spec, {
                    "phase": "final_response", "iteration": iteration, "model": spec.model,
                    "assistant_message": assistant_message,
                    "completed_tool_results": [], "pending_tool_calls": [],
                })
                messages.append({"role": "user", "content": CORRECTION, "_hidden_history": True})
                return clean, True
            stop_reason = "incomplete"
            assistant_message["content"] = INCOMPLETE
            return INCOMPLETE, False

        governance_config = ContextGovernanceConfig(
            provider=self.provider,
            model=spec.model,
            tools=spec.tools,
            workspace=spec.workspace,
            session_key=spec.session_key,
            max_tool_result_chars=spec.max_tool_result_chars,
            context_window_tokens=spec.context_window_tokens,
            context_block_limit=spec.context_block_limit,
            max_tokens=spec.max_tokens,
            inflight_start_index=len(spec.initial_messages),
        )

        for iteration in range(spec.max_iterations):
            if spec.selected_context and not spec.selected_context.legacy_fallback:
                spec.selected_context.control.refresh()
                if spec.selected_context.control.cancelled:
                    final_content, stop_reason = spec.selected_context.verdict(None, "cancelled")
                    self._append_final_message(messages, final_content)
                    break
                # t54: whole-request budget / repeated refused action -> stop with a code-built report
                _budget = getattr(spec.selected_context.trace, "budget", None)
                _stop = getattr(spec.selected_context, "stop_requested", None) or (
                    _budget.exhausted() if _budget is not None else None)
                if _stop:
                    if _budget is not None:
                        _budget.stop(_stop)
                    final_content, stop_reason = spec.selected_context.verdict(None, _stop)
                    self._append_final_message(messages, final_content)
                    break
            try:
                # Keep the persisted conversation untouched. Context governance
                # may repair or compact historical messages for the model, but
                # those synthetic edits must not shift the append boundary used
                # later when the caller saves only the new turn.
                if spec.selected_context:
                    from nanobot.agent.context_selection import prepare_selected_loop
                    messages_for_model = prepare_selected_loop(spec, messages, self.provider)
                else:
                    messages_for_model = self.context_governor.prepare_for_model(
                        governance_config, messages, compacted_tool_call_ids,
                    )
            except ContextCapacityError:
                if spec.selected_context:
                    final_content = ("필수 문맥이 모델 입력 한도를 초과했습니다. 원본을 보존한 채 "
                                     "작업 분할 또는 명시적인 입력 한도 확장이 필요합니다.")
                    stop_reason = "incomplete"
                    self._append_final_message(messages, final_content)
                    break
                raise
            except Exception:
                if spec.selected_context:
                    spec.selected_context.trace.count("recovery", "selection_loop_validation_failed")
                    final_content = "문맥 선택 검증에 실패해 실행을 중단했습니다. 원본은 보존됩니다."
                    stop_reason = "incomplete"
                    self._append_final_message(messages, final_content)
                    break
                logger.exception(
                    "Context governance failed on turn {} for {}; applying minimal repair",
                    iteration,
                    spec.session_key or "default",
                )
                try:
                    messages_for_model = ContextGovernor.strip_placeholder_assistant_messages(
                        messages
                    )
                    messages_for_model = ContextGovernor.strip_malformed_tool_calls(
                        messages_for_model
                    )
                    messages_for_model = ContextGovernor.drop_orphan_tool_results(
                        messages_for_model
                    )
                    messages_for_model = ContextGovernor.backfill_missing_tool_results(
                        messages_for_model
                    )
                except Exception:
                    messages_for_model = messages
            context = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(context)
            response = await self._request_model(spec, messages_for_model, hook, context)
            context.response = response
            context.tool_calls = list(response.tool_calls)

            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            response.content = RepeatedSentenceGuard().finish(cleaned_content)
            raw_usage = self._usage_or_estimate(spec, messages_for_model, response)
            context.usage = dict(raw_usage)
            self._accumulate_usage(usage, raw_usage)
            if reasoning_text and not context.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            if response.should_execute_tools:
                context.tool_calls = list(response.tool_calls)
                structured_report = spec.task_state is not None and any(
                    call.name == "update_task" and call.arguments.get("status") == "completed"
                    and str(call.arguments.get("final_answer") or "").strip()
                    for call in response.tool_calls
                )
                observability_final = (
                    (not is_blank_text(response.content) or structured_report)
                    and self._all_tool_calls_observability(spec, response.tool_calls)
                )
                if hook.wants_streaming() and not observability_final:
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)
                pending_before_tools = []
                if spec.task_state is not None or spec.selected_context:
                    pending_before_tools = await self._drain_injections(spec, wait=not bool(spec.selected_context))
                if pending_before_tools:
                    # Complete the old tool protocol without executing a now-stale action.
                    results = ["Superseded by a new user instruction; reinterpret before execution."
                               for _ in response.tool_calls]
                    new_events = [{"name": call.name, "status": "superseded"}
                                  for call in response.tool_calls]
                    fatal_error = None
                    had_injections = True
                    injection_cycles += 1
                else:
                    results, new_events, fatal_error = await self._execute_tools(
                        spec, response.tool_calls, external_lookup_counts, workspace_violation_counts,
                    )
                    if spec.task_state is not None and spec.task_state.pending_messages:
                        pending_before_tools = spec.task_state.pending_messages
                        spec.task_state.pending_messages = []
                        had_injections = True
                        injection_cycles += 1
                if spec.selected_context and spec.selected_context.control.pending:
                    pending_before_tools.extend(spec.selected_context.control.pending)
                    spec.selected_context.control.pending = []
                    had_injections = True
                tool_events.extend(new_events)
                tools_used.extend(
                    tool_call.name
                    for tool_call, event in zip(response.tool_calls, new_events)
                    if event.get("status") == "ok"
                )
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                if spec.selected_context and hasattr(spec.tools, "resolver"):
                    for call, event, observed_result in zip(response.tool_calls, new_events, results):
                        tool = spec.tools.get(call.name)
                        if call.name not in {"discover_context_capabilities", "resolve_context_need",
                                              "record_context_completion", "revise_context_requirements", "skill_decision",
                                              "skill_search", "skill_registry"}:
                            spec.selected_context.capture(call, observed_result, event.get("status"), tool,
                                                          executed=event.get("executed", True))
                        elif call.name == "record_context_completion" and event.get("status") != "ok":
                            from nanobot.agent.context_plan import note_record_error
                            note_record_error(spec.selected_context, call, observed_result)
                        if call.name in {"search_memory", "read_memory_events"}:
                            spec.selected_context.trace.count("additional_searches", "executor_plan_revision")
                        spec.tools.resolver.observed[call.id] = bool(
                            tool and tool.read_only and event.get("status") == "ok"
                            and str(observed_result).strip()
                            and not str(observed_result).lower().startswith(("no scoped", "no memory", "error:"))
                            and call.name not in {"discover_context_capabilities", "resolve_context_need",
                                                  "record_context_completion", "revise_context_requirements", "skill_decision", "skill_search", "skill_registry"})
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result if spec.selected_context else self.context_governor.normalize_tool_result(
                            governance_config,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    if spec.selected_context:
                        observation = spec.selected_context.observations.get(tool_call.id, {})
                        tool_message["execution_status"] = observation.get("status", "unknown")
                        tool_message["read_only"] = observation.get("read_only", True)
                        if observation.get("source_event_id"):
                            tool_message["memory_event_id"] = observation["source_event_id"]
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if pending_before_tools:
                    self._append_injected_messages(messages, pending_before_tools)
                    await hook.after_iteration(context)
                    continue
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                if observability_final:
                    clean = hook.finalize_content(context, response.content)
                    await self._emit_checkpoint(spec, {
                        "phase": "tools_completed", "iteration": iteration, "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results, "pending_tool_calls": [],
                    })
                    clean, guard_resume = await guard_final(
                        clean, assistant_message, iteration, already_appended=True,
                    )
                    if guard_resume:
                        drained, injection_cycles = await self._try_drain_injections(
                            spec, messages, None, injection_cycles, phase="after final correction",
                        )
                        had_injections = had_injections or drained
                        if hook.wants_streaming():
                            await hook.on_stream_end(context, resuming=True)
                        await hook.after_iteration(context)
                        continue
                    # Match the ordinary final path's injection/goal behavior.
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles, phase="after observability final",
                    )
                    if not should_continue and stop_reason != "incomplete":
                        predicate = spec.goal_active_predicate
                        if predicate is not None and predicate():
                            messages.append(self._build_goal_continue_message(spec))
                            should_continue = True
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=should_continue)
                    if should_continue:
                        had_injections = True
                        await hook.after_iteration(context)
                        continue
                    await self._emit_checkpoint(
                        spec,
                        {
                            "phase": "final_response",
                            "iteration": iteration,
                            "model": spec.model,
                            "assistant_message": assistant_message,
                            "completed_tool_results": completed_tool_results,
                            "pending_tool_calls": [],
                        },
                    )
                    final_content = clean
                    context.final_content = final_content
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0
                length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                allocation = getattr(spec.selected_context, "allocation", None)
                if (not _drained and spec.task_state is None and allocation is not None
                        and allocation.decision == "pass"
                        and any(event.get("name") == "submit_time_allocation" for event in new_events)
                        and all(event.get("status") == "ok" and event.get("executed", True)
                                for event in new_events)):
                    # A verified structured result already contains the answer. Do not
                    # spend another model iteration just to trigger its final verdict.
                    # Refresh/revalidate here; the loop's save boundary still commits
                    # the exact output and DONE atomically before publishing either.
                    content, status = allocation.finalize(None)
                    if status == "completed":
                        final_content, stop_reason = content, status
                        self._append_final_message(messages, final_content)
                        context.final_content, context.stop_reason = final_content, stop_reason
                        if hook.wants_streaming():
                            await hook.on_stream_end(context, resuming=False)
                        await hook.after_iteration(context)
                        break
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            if response.finish_reason != "error" and is_blank_text(clean):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                retry_messages = self._finalization_retry_messages(messages_for_model)
                response = await self._request_finalization_retry(spec, messages_for_model)
                retry_usage = self._usage_or_estimate(spec, retry_messages, response)
                self._accumulate_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_length_recovery_message())
                    await hook.after_iteration(context)
                    continue

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            if assistant_message is not None:
                clean, guard_resume = await guard_final(clean, assistant_message, iteration)
                if guard_resume:
                    drained, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles, phase="after final correction",
                    )
                    had_injections = had_injections or drained
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    await hook.after_iteration(context)
                    continue

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
                allow_goal_continue=stop_reason != "incomplete",
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if (response.finish_reason == "error" and response.error_kind == "budget_exhausted"
                    and spec.selected_context and not spec.selected_context.legacy_fallback):
                # t54: the send was refused by the request budget (nothing sent): stop, no extra call.
                _budget = getattr(spec.selected_context.trace, "budget", None)
                _stop = (_budget.stop_reason if _budget is not None else None) or "budget_sends"
                final_content, stop_reason = spec.selected_context.verdict(None, _stop)
                self._append_final_message(messages, final_content)
                break
            if response.finish_reason == "error":
                if LLMProvider.is_arrearage_response(response):
                    final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(assistant_message or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We include them before the no-tools finalization pass so the
            # final response can account for every known follow-up.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True
            final_content = None
            if spec.task_state is not None and spec.task_state.update is not None:
                spec.task_state.update.final_answer = hook.finalize_content(
                    AgentHookContext(iteration=spec.max_iterations, messages=messages),
                    spec.task_state.update.final_answer,
                ) or ""
            if (spec.task_state is not None and not spec.task_state.defects()
                    and spec.task_state.update.status == "completed"):
                final_content = spec.task_state.update.final_answer.strip()
            if final_content is None and spec.finalize_on_max_iterations and not spec.selected_context:
                final_content = await self._try_finalize_after_max_iterations(
                    spec,
                    hook,
                    messages,
                    usage,
                )
            if final_content is None:
                final_content = self._max_iterations_fallback(spec)
            if spec.premature_final_guard and is_premature_final(
                final_content, messages, spec.tools, tool_events,
                spec.premature_final_session_metadata,
            ):
                final_content = INCOMPLETE
            if spec.task_state is not None:
                defects, terminal = spec.task_state.final_check()
                if defects and not terminal:
                    final_content = spec.task_state.incomplete(defects)
                elif not terminal:
                    final_content = spec.task_state.update.final_answer.strip()
                    stop_reason = "completed"
                elif terminal:
                    final_content = str(final_content or "") + "\n\n미완료 상태: " + spec.task_state.update.explanation
            if spec.premature_final_guard and needs_answer_body(final_content, messages):
                final_content = str(final_content or "") + "\n\n" + ANSWER_BODY_INCOMPLETE
                if stop_reason == "completed":
                    stop_reason = "incomplete"
            if spec.selected_context and not spec.selected_context.legacy_fallback:
                final_content, stop_reason = spec.selected_context.verdict(final_content, "max_iterations")
            self._append_final_message(messages, final_content)

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
        )

    @staticmethod
    def _all_tool_calls_observability(
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> bool:
        if not tool_calls:
            return False
        for tool_call in tool_calls:
            if (spec.task_state is not None and tool_call.name == "update_task"
                    and tool_call.arguments.get("status") == "active"):
                return False
            tool = spec.tools.get(tool_call.name)
            if tool is None or getattr(tool, "observability", False) is not True:
                return False
        return True

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        *,
        malformed_retry: bool = False,
    ):
        if spec.task_state is not None:
            spec.task_state.model_calls += 1
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set NANOBOT_LLM_TIMEOUT_S=0 to disable.
            raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_definitions(),
        )
        # Selected execution must validate every final candidate before delivery.
        # Content deltas could otherwise publish an unsupported success claim
        # even when verdict() subsequently rejects the final response.
        gate_delivery = bool(spec.selected_context and not spec.selected_context.legacy_fallback)
        wants_streaming = hook.wants_streaming() and not gate_delivery
        wants_progress_streaming = (
            not wants_streaming
            and not gate_delivery
            and spec.stream_progress_deltas
            and spec.progress_callback is not None
            and getattr(self.provider, "supports_progress_deltas", False) is True
        )

        progress_state: dict[str, bool] | None = None
        live_file_edits: StreamingFileEditTracker | None = None

        if (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        ):
            async def _emit_live_file_edits(events: list[dict[str, Any]]) -> None:
                await invoke_file_edit_progress(spec.progress_callback, events)

            live_file_edits = StreamingFileEditTracker(
                workspace=spec.workspace,
                tools=spec.tools,
                emit=_emit_live_file_edits,
            )

        async def _tool_call_delta(delta: dict[str, Any]) -> None:
            if live_file_edits is not None:
                await live_file_edits.update(delta)

        if wants_streaming:
            thinking_buf = ""
            repetition = RepeatedSentenceGuard()

            async def _stream(delta: str) -> None:
                visible = repetition.feed(delta)
                if visible:
                    context.streamed_content = True
                    await hook.on_stream(context, visible)

            async def _thinking(delta: str) -> None:
                nonlocal thinking_buf
                if not delta:
                    return
                prev_clean = strip_reasoning_tags(thinking_buf)
                thinking_buf += delta
                new_clean = strip_reasoning_tags(thinking_buf)
                incremental = new_clean[len(prev_clean):]
                if incremental:
                    context.streamed_reasoning = True
                    await hook.emit_reasoning(incremental)

            async def _stream_recover() -> None:
                await hook.on_stream_end(context, resuming=True)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
                on_stream_recover=_stream_recover,
            )
        elif wants_progress_streaming:
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    await spec.progress_callback(incremental)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream_progress,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
            )
        else:
            coro = self.provider.chat_with_retry(**kwargs)

        # Streaming requests already have provider-level idle timeouts
        # (NANOBOT_STREAM_IDLE_TIMEOUT_S). Do not also apply the outer wall-clock
        # LLM timeout here, or healthy long reasoning streams can be killed just
        # because total elapsed time exceeded NANOBOT_LLM_TIMEOUT_S.
        outer_timeout_s = None if (wants_streaming or wants_progress_streaming) else timeout_s
        try:
            response = (
                await coro if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
            if live_file_edits is not None:
                await live_file_edits.flush()
                if response.should_execute_tools:
                    live_file_edits.apply_final_call_ids(response.tool_calls)
                await live_file_edits.error_unmatched(
                    response.tool_calls if response.should_execute_tools else [],
                    "Tool call did not complete.",
                )
        except asyncio.TimeoutError:
            if spec.selected_context:
                spec.selected_context.trace.mark_timeout()
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        dropped, all_dropped, original_finish_reason = (
            self._drop_malformed_tool_calls(response)
        )
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and not malformed_retry
        ):
            logger.warning(
                "Retrying LLM request after all {} malformed tool call(s) were dropped",
                dropped,
            )
            retry_messages = self._malformed_tool_call_retry_messages(
                messages, response.content,
            )
            return await self._request_model(
                spec, retry_messages, hook, context,
                malformed_retry=True,
            )
        if (
            all_dropped
            and original_finish_reason in ("tool_calls", "function_call")
            and malformed_retry
        ):
            logger.warning(
                "Malformed tool calls persisted after retry; falling back to no-tools request",
            )
            fallback_messages = self._malformed_tool_call_retry_messages(
                messages, response.content,
            )
            return await self._request_no_tools(spec, fallback_messages)
        return response

    @staticmethod
    def _drop_malformed_tool_calls(
        response: LLMResponse,
    ) -> tuple[int, bool, str | None]:
        """Strip tool calls whose name is missing/non-string from the response.

        Returns (dropped_count, all_dropped, original_finish_reason).

        A degenerate call (name=None or "") cannot be executed, and if it were
        persisted into the assistant message it would be replayed on every
        subsequent turn, causing upstream validation errors
        (``tool_use.name: Input should be a valid string``) that permanently
        wedge the session. Dropping it here keeps it out of execution, the
        assistant message, and the saved history in one place.
        """
        calls = getattr(response, "tool_calls", None)
        if not calls:
            return (0, False, getattr(response, "finish_reason", None))
        valid = [tc for tc in calls if tc.has_valid_name()]
        if len(valid) == len(calls):
            return (0, False, getattr(response, "finish_reason", None))
        dropped = len(calls) - len(valid)
        original_finish_reason = getattr(response, "finish_reason", None)
        logger.warning(
            "Dropped {} malformed tool call(s) with missing/non-string name "
            "from LLM response (finish_reason={!r})",
            dropped,
            original_finish_reason,
        )
        response.tool_calls = valid
        if not valid:
            response.finish_reason = "stop"
        return (dropped, not valid, original_finish_reason)

    @staticmethod
    def _malformed_tool_call_retry_messages(
        messages: list[dict[str, Any]],
        assistant_text: str | None,
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        note = (
            "The previous model response attempted to call tools, but every tool call "
            "was malformed: the tool_use blocks had missing or non-string tool names. "
            "Do not answer with a promise to use tools. Either call the required tools again "
            "using valid tool names from the provided tool list and JSON object inputs, or give "
            "a final answer only if no tool is required."
        )
        if assistant_text:
            note += (
                f"\n\nPrevious assistant text before the malformed calls:\n"
                f"{assistant_text}"
            )
        retry_messages.append({"role": "user", "content": note})
        return retry_messages

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        retry_messages = self._finalization_retry_messages(messages)
        return await self._request_no_tools(spec, retry_messages)

    @staticmethod
    def _finalization_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        return retry_messages

    async def _try_finalize_after_max_iterations(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> str | None:
        retry_messages = self._budget_exhausted_finalization_messages(messages)
        try:
            response = await self._request_no_tools(spec, retry_messages)
        except Exception:
            logger.exception(
                "Budget-exhausted finalization failed for {}; using fallback",
                spec.session_key or "default",
            )
            return None

        raw_usage = self._usage_or_estimate(spec, retry_messages, response)
        self._accumulate_usage(usage, raw_usage)
        if response.finish_reason == "error" or response.has_tool_calls:
            logger.warning(
                "Budget-exhausted finalization returned finish_reason='{}' "
                "with {} tool call(s) for {}; using fallback",
                response.finish_reason,
                len(response.tool_calls),
                spec.session_key or "default",
            )
            return None

        context = AgentHookContext(
            iteration=spec.max_iterations,
            messages=messages,
            response=response,
            usage=dict(raw_usage),
            session_key=spec.session_key,
        )
        clean = hook.finalize_content(context, response.content)
        if is_blank_text(clean):
            return None
        return clean

    async def _request_no_tools(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        if spec.task_state is not None:
            spec.task_state.model_calls += 1
        kwargs = self._build_request_kwargs(spec, messages, tools=None)
        return await self.provider.chat_with_retry(**kwargs)

    @staticmethod
    def _budget_exhausted_finalization_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_budget_exhausted_finalization_message())
        return retry_messages

    @staticmethod
    def _max_iterations_fallback(spec: AgentRunSpec) -> str:
        if spec.max_iterations_message:
            return spec.max_iterations_message.format(
                max_iterations=spec.max_iterations,
            )
        return render_template(
            "agent/max_iterations_message.md",
            strip=True,
            max_iterations=spec.max_iterations,
        )

    def _usage_or_estimate(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        usage = self._usage_dict(response.usage)
        total = self._usage_total(usage)
        if total > 0:
            usage["total_tokens"] = total
            usage.setdefault("provider_tokens", total)
            return usage
        if response.finish_reason == "error":
            return {}
        return self._estimate_response_usage(spec, messages, response)

    def _estimate_response_usage(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        try:
            tools = spec.tools.get_definitions()
        except Exception:
            tools = None
        prompt_tokens, _ = estimate_prompt_tokens_chain(self.provider, spec.model, messages, tools)
        assistant_message = build_assistant_message(
            response.content or "",
            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        completion_tokens = estimate_message_tokens(assistant_message)
        total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        if total_tokens <= 0:
            return {}
        return {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
            "total_tokens": total_tokens,
            "estimated_tokens": total_tokens,
        }

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _usage_total(usage: dict[str, int]) -> int:
        return max(0, usage.get("total_tokens", 0) or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        ))

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        # t49: a plan/requirement update sent with other actions is processed first.
        # t50: a rejected update returns only its own error; the other actions still run, each through
        # its own pre-execution checks (repeat, permission, target, cancellation, budget). An update
        # grants no permission, so nothing depends on its acceptance to be safe.
        first: dict[int, tuple[Any, dict[str, str], BaseException | None]] = {}
        if spec.selected_context:
            for i, call in enumerate(tool_calls):
                args = call.arguments if isinstance(call.arguments, dict) else {}
                if call.name == "revise_context_requirements" and args.get("reason") in {
                        "update_plan", "supplement_requirement"}:
                    first[i] = await self._run_tool(spec, call, external_lookup_counts, workspace_violation_counts)
        if first:
            if any(first[i][1].get("status") != "ok" for i in first):
                spec.selected_context.trace.count("plan_gate", "update_rejected_others_run")
            rest = [c for i, c in enumerate(tool_calls) if i not in first]
            rest_res, rest_ev, rest_err = await self._execute_tools(
                spec, rest, external_lookup_counts, workspace_violation_counts) if rest else ([], [], None)
            rest_out = (list(zip(rest_res, rest_ev, [None] * len(rest_res))), rest_err)
            merged, it = [], iter(rest_out[0])
            for i in range(len(tool_calls)):
                merged.append(first[i] if i in first else next(it))
            fatal = next((e for _, _, e in merged if e is not None), rest_out[1])
            return [r for r, _, _ in merged], [e for _, e, _ in merged], fatal
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.task_state is not None:
                followups = await self._drain_injections(spec)
                spec.task_state.pending_messages.extend(followups)
                if spec.task_state.pending_messages:
                    tool_results.extend((
                        "Superseded by a new user instruction; reinterpret before execution.",
                        {"name": call.name, "status": "superseded"}, None,
                    ) for call in batch)
                    continue
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(*(
                    self._run_tool(
                        spec, tool_call, external_lookup_counts, workspace_violation_counts,
                    )
                    for tool_call in batch
                ))
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await self._run_tool(
                        spec, tool_call, external_lookup_counts, workspace_violation_counts,
                    )
                    tool_results.append(result)
                    batch_results.append(result)

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(self, spec, tool_call, external_lookup_counts, workspace_violation_counts):
        tool = spec.tools.get(tool_call.name)
        async def operation():
            return await self._run_tool_with_task_note(
                spec, tool_call, external_lookup_counts, workspace_violation_counts)
        if spec.selected_context and tool is not None and not tool.read_only:
            return await spec.selected_context.control.run_mutation(
                tool_call, tool, operation, lambda: self._drain_injections(spec, wait=False))
        return await operation()

    async def _run_tool_with_task_note(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        if spec.task_state is None:
            return await self._run_tool_unchecked(
                spec, tool_call, external_lookup_counts, workspace_violation_counts,
            )
        from nanobot.agent.task_note import bounded_operation
        from nanobot.agent.tools.base import ToolResult

        note = spec.task_state
        tool = spec.tools.get(tool_call.name)
        if tool_call.name != "update_task" and not bool(getattr(tool, "read_only", False)):
            blocked = note.mutation_error(tool_call.name, tool_call.arguments)
            if blocked:
                return ToolResult.error(blocked), {
                    "name": tool_call.name, "status": "blocked", "detail": blocked,
                }, None
        if tool is not None and spec.progress_callback is not None:
            notice = note.progress.operation(tool_call.name, tool_call.arguments)
            if notice:
                await spec.progress_callback(notice, tool_hint=False)
        outcome = await bounded_operation(note, tool_call.name, lambda: self._run_tool_unchecked(
            spec, tool_call, external_lookup_counts, workspace_violation_counts,
        ))
        if isinstance(outcome, tuple):
            result, event, error = outcome
        else:
            result, event, error = outcome, {"name": tool_call.name, "status": "unknown",
                                            "detail": str(outcome)}, None
        if tool_call.name == "update_task" and event["status"] == "ok" and spec.progress_callback is not None:
            notice = note.progress.plan(note.update)
            if notice:
                await spec.progress_callback(notice, tool_hint=False)
        if tool_call.name != "update_task":
            if (getattr(result, "read_evidence", None) and isinstance(result, str)
                    and len(result) > spec.max_tool_result_chars):
                # The model receives an offload/preview, not the full returned document.
                result.read_evidence["extraction_ok"] = False
                result.read_evidence["context_truncated"] = True
            note.observe(tool_call.name, tool_call.arguments, result, tool, event["status"], tool_call.id)
        return result, event, error

    async def _run_tool_unchecked(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        from nanobot.agent.tools.base import ToolResult

        hint = "\n\n[Analyze the error above and try a different approach.]"
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if spec.fail_on_tool_error:
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            with suppress(Exception):
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
        if prep_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
                "executed": False,   # t49: refused before execution
            }
            handled = self._classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            return prep_error + hint, event, (
                RuntimeError(prep_error) if spec.fail_on_tool_error else None
            )
        emit_file_edit_events = (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        )
        progress_callback = spec.progress_callback if emit_file_edit_events else None
        file_edit_trackers = (
            prepare_file_edit_trackers(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                tool=tool,
                workspace=spec.workspace,
                params=params if isinstance(params, dict) else None,
            )
            if progress_callback is not None
            else None
        )
        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_start_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )
        try:
            checked_versions = spec.selected_context.watched_versions() if spec.selected_context else {}
            if tool is not None:
                if spec.selected_context and not tool.read_only:
                    latest = await self._drain_injections(spec, wait=False)
                    spec.selected_context.control.pending.extend(latest)
                    error = spec.selected_context.control.authorize(
                        tool_call.name, (tool_call.arguments or {}).get("path")
                        if isinstance(tool_call.arguments, dict) else None)
                    if error or latest:
                        return ToolResult.error(error or "Prepared action superseded"), {
                            "name": tool_call.name, "status": "blocked", "executed": False}, None
                    spec.selected_context.control.active = tool_call.id
                    spec.selected_context.control.execution(tool_call, "running")
                from nanobot.agent.tools.search import FindFilesTool, GrepTool

                if spec.task_state is not None and type(tool) in {FindFilesTool, GrepTool}:
                    from nanobot.agent.tools.search_worker import isolated_search

                    result = await isolated_search(tool, params)
                else:
                    _budget = getattr(spec.selected_context.trace, "budget", None) if spec.selected_context else None
                    _limit = _budget.cap(None) if _budget is not None else None
                    if _limit is not None:
                        # t54: a tool wait never outlives the request deadline. A timeout goes through the
                        # error path below; a change it may have made stays "unknown" (no rollback).
                        try:
                            async with asyncio.timeout(_limit):
                                result = await tool.execute(**params)
                        except TimeoutError:
                            _budget.stop("budget_deadline")
                            from nanobot.agent.tools.shell import termination_note
                            info = getattr(tool, "last_termination", None)
                            note = termination_note(info)
                            if note and spec.selected_context is not None:
                                spec.selected_context.unconfirmed_terminations = [
                                    *getattr(spec.selected_context, "unconfirmed_terminations", []),
                                    dict(info, call=tool_call.id)]
                            raise TimeoutError("request deadline reached while waiting for " + tool_call.name
                                               + ("; " + note if note else ""))
                    else:
                        result = await tool.execute(**params)
            else:
                _budget = getattr(spec.selected_context.trace, "budget", None) if spec.selected_context else None
                _limit = _budget.cap(None) if _budget is not None else None
                if _limit is not None:
                    try:
                        async with asyncio.timeout(_limit):
                            result = await spec.tools.execute(tool_call.name, params)
                    except TimeoutError:
                        _budget.stop("budget_deadline")
                        raise TimeoutError("request deadline reached while waiting for " + tool_call.name)
                else:
                    result = await spec.tools.execute(tool_call.name, params)
            execution = getattr(result, "execution_evidence", None)
            if spec.selected_context and isinstance(execution, dict):
                execution["checked_versions"] = checked_versions
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, str(exc))
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            payload = f"Error: {type(exc).__name__}: {exc}"
            handled = self._classify_violation(
                raw_text=str(exc),
                # Preserve legacy exception payloads without the retry hint.
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None

        if is_tool_error_result(tool_call.name, result):
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, result)
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            handled = self._classify_violation(
                raw_text=result,
                soft_payload=result + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return result + hint, event, RuntimeError(result)
            return result + hint, event, None

        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_end_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

    # SSRF is a hard security block at the tool boundary, but the agent turn
    # should recover conversationally instead of aborting the runtime.
    _SSRF_MARKERS: tuple[str, ...] = (
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )

    # Non-SSRF boundary markers returned to the LLM as recoverable tool errors.
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """True when *text* looks like any policy boundary rejection."""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, str],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """Classify safety-boundary failures, or return ``None`` to pass through."""
        if self._is_ssrf_violation(raw_text):
            logger.warning(
                "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
                tool_call.name,
                raw_text.replace("\n", " ").strip()[:200],
            )
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            return self._ssrf_soft_payload(raw_text), event, None

        if self._is_workspace_violation(raw_text):
            escalation = repeated_workspace_violation_error(
                tool_call.name,
                tool_call.arguments,
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            if escalation is not None:
                logger.warning(
                    "Tool {} hit workspace boundary repeatedly; escalating hint",
                    tool_call.name,
                )
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ",
                    raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None

        return None

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches

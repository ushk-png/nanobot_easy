"""WorkflowService for stage-3 connection and lifecycle.

This is intentionally conservative: it persists task envelopes and supports
run/status/list/cancel plus deterministic resume of a waiting task into a final
answer. Restart recovery never auto-replays RUNNING work when external side
effects may be unclear; expired leases are surfaced for operator attention.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import OutboundMessage
from nanobot.workflow.composition import compose_workflow_definition
from nanobot.workflow.context_builder import build_workflow_context_snapshot
from nanobot.workflow.delivery import set_workflow_turn_delivery
from nanobot.workflow.evaluation import WorkflowEvaluator
from nanobot.workflow.executor import WorkflowExecutor
from nanobot.workflow.llm_adapter import WorkflowLLMAdapter
from nanobot.workflow.schema import WorkflowDefinition, WorkflowEnvelope, WorkflowPrincipal
from nanobot.workflow.store import WorkflowStore
from nanobot.workflow.access import WorkflowAccessDenied, ensure_task_access

_TERMINAL_STATES = {"COMPLETED", "FAILED", "NEEDS_ATTENTION", "CANCELLED", "WAITING_USER"}
_LEASE_SECONDS = 300


class WorkflowService:
    def __init__(
        self,
        *,
        workspace: str | Path,
        provider_loader: Any | None,
        bus: Any | None = None,
        sync_wait_seconds: int = 60,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.provider_loader = provider_loader
        self.bus = bus
        self.sync_wait_seconds = sync_wait_seconds
        self.store = WorkflowStore(self.workspace)
        self.evaluator = WorkflowEvaluator()
        self._running = False
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self._running = True
        self._recover_expired_leases()

    async def stop(self) -> None:
        self._running = False
        if self._tasks:
            for task in self._tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()

    def waiting_tasks_for_principal(self, principal: WorkflowPrincipal) -> list[dict[str, Any]]:
        return self.store.list_tasks(principal, states={"WAITING_USER"})

    def _recover_expired_leases(self) -> None:
        """Mark abandoned RUNNING tasks for attention without replaying work.

        A process restart loses in-memory executor state. Because a RUNNING
        workflow may already have invoked external tools before the process died,
        automatically replaying from the last persisted input could duplicate
        side effects or overwrite an unclear result. Until step-level idempotent
        checkpoints exist, startup recovery is therefore conservative: expired
        RUNNING leases become NEEDS_ATTENTION and require explicit operator/user
        action.
        """
        for row in self.store.list_expired_leases(now=time.time(), states={"RUNNING"}):
            task_id = str(row.get("task_id") or "")
            if not task_id:
                continue
            previous = WorkflowEnvelope.model_validate(row.get("envelope") or {})
            envelope = WorkflowEnvelope(
                task_id=task_id,
                state="NEEDS_ATTENTION",
                delivery_state=previous.delivery_state,
                reason="workflow restart recovery requires attention; expired running lease was not automatically replayed",
                next_hint="Inspect prior trace/data before deciding whether to retry, cancel, or recreate the workflow.",
            )
            self.store.upsert_task(
                task_id=task_id,
                principal=WorkflowPrincipal.model_validate(row.get("principal") or {}),
                definition_id=row.get("definition_id") or "situation_judgment.v1",
                envelope=envelope,
                context=row.get("context") or {},
                data=row.get("data") or {},
                trace=row.get("trace") or [],
                question_id=row.get("question_id"),
                resume_next=row.get("resume_next"),
            )

    def list_definitions(self) -> list[dict[str, Any]]:
        definition = self._load_definition("situation_judgment.v1")
        return [{
            "id": definition.id,
            "version": definition.version,
            "description": definition.description,
            "requires": sorted(definition.referenced_tools),
        }]

    async def run(
        self,
        *,
        principal: WorkflowPrincipal,
        user_text: str,
        registry: ToolRegistry,
        workflow_id: str = "situation_judgment.v1",
        session_metadata: dict[str, Any] | None = None,
        recent_history: list[dict[str, Any]] | None = None,
        goal: str | None = None,
        user_text_source: str | None = None,
    ) -> WorkflowEnvelope:
        provider = self.provider_loader() if self.provider_loader else None
        if provider is None:
            return WorkflowEnvelope(state="NEEDS_ATTENTION", reason="workflow run requires an active provider")
        base_definition = self._load_definition(workflow_id)
        definition, composition = compose_workflow_definition(
            base_definition,
            available_tools=set(registry.tool_names),
            request_text=user_text,
        )
        context = build_workflow_context_snapshot(
            user_text=user_text,
            principal=principal,
            session_metadata=session_metadata or {},
            recent_history=recent_history or [],
            tools=registry,
            same_session_workflows=self._same_session_summary(principal),
        )
        context["workflow_composition"] = composition.model_dump()
        if goal is not None:
            context["goal"] = goal
        if user_text_source is not None:
            context["user_text_source"] = user_text_source
        executor = WorkflowExecutor(
            definition=definition,
            tools=registry,
            llm=WorkflowLLMAdapter(provider),
            context_snapshot=context,
        )
        running = WorkflowEnvelope(task_id=executor.task_id, state="RUNNING", reason="workflow execution started")
        self.store.upsert_task(
            task_id=executor.task_id,
            principal=principal,
            definition_id=definition.id,
            envelope=running,
            context=context,
        )
        self.store.set_lease(executor.task_id, lease_until=time.time() + _LEASE_SECONDS)
        coro = self._execute_and_persist(executor, principal, definition, context)
        if self.sync_wait_seconds <= 0:
            task = asyncio.create_task(coro)
            self._tasks[executor.task_id] = task
            task.add_done_callback(lambda t, task_id=executor.task_id: self._tasks.pop(task_id, None))
            return running
        try:
            return await asyncio.wait_for(asyncio.shield(coro), timeout=self.sync_wait_seconds)
        except asyncio.TimeoutError:
            task = asyncio.create_task(coro)
            self._tasks[executor.task_id] = task
            task.add_done_callback(lambda t, task_id=executor.task_id: self._tasks.pop(task_id, None))
            return running

    async def _execute_and_persist(
        self,
        executor: WorkflowExecutor,
        principal: WorkflowPrincipal,
        definition: WorkflowDefinition,
        context: dict[str, Any],
    ) -> WorkflowEnvelope:
        try:
            result = await executor.run()
            result, evaluation = self.evaluator.evaluate(result)
            result.data.setdefault("evaluation", evaluation.model_dump())
        except Exception as exc:
            envelope = WorkflowEnvelope(task_id=executor.task_id, state="FAILED", reason=str(exc))
            self.store.upsert_task(
                task_id=executor.task_id,
                principal=principal,
                definition_id=definition.id,
                envelope=envelope,
                context=context,
            )
            return envelope
        q = (result.data.get("results") or {}).get("user_question") or {}
        self.store.upsert_task(
            task_id=executor.task_id,
            principal=principal,
            definition_id=definition.id,
            envelope=result.envelope,
            context=context,
            data=result.data,
            trace=result.trace,
            question_id=result.envelope.question_id,
            resume_next=q.get("resume_next") if isinstance(q, dict) else None,
        )
        if result.envelope.state in {"COMPLETED", "WAITING_USER"}:
            content = result.envelope.reason if result.envelope.state == "COMPLETED" else result.envelope.question
            if content:
                set_workflow_turn_delivery(result.envelope.task_id or executor.task_id, content, kind=result.envelope.deliver)
        return result.envelope

    def status(self, *, principal: WorkflowPrincipal, task_id: str | None = None) -> WorkflowEnvelope | list[dict[str, Any]]:
        if not task_id:
            return [self._summary(row) for row in self.store.list_tasks(principal)]
        row = self.store.get_task(task_id)
        if row is None:
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow task is not available")
        try:
            ensure_task_access(principal, row)
        except WorkflowAccessDenied:
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow task is not available")
        return WorkflowEnvelope.model_validate(row["envelope"])

    async def resume(
        self,
        *,
        principal: WorkflowPrincipal,
        task_id: str,
        question_id: str | None,
        answer: str,
        registry: ToolRegistry,
        answer_source: str | None = None,
        input_reference: str | None = None,
    ) -> WorkflowEnvelope:
        row = self.store.get_task(task_id)
        if row is None:
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow task is not available")
        try:
            ensure_task_access(principal, row)
        except WorkflowAccessDenied:
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow task is not available")
        if row["state"] != "WAITING_USER":
            return WorkflowEnvelope.model_validate(row["envelope"])
        if question_id and row.get("question_id") and question_id != row.get("question_id"):
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow question is not available")
        provider = self.provider_loader() if self.provider_loader else None
        if provider is None:
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow resume requires an active provider")
        definition = self._load_definition(row.get("definition_id") or "situation_judgment.v1")
        available_tools = set(registry.tool_names)
        missing_required = sorted(
            tool_name for tool_name in definition.referenced_tools
            if tool_name not in available_tools and self._tool_still_required_after_resume(definition, tool_name)
        )
        if missing_required:
            envelope = WorkflowEnvelope(
                task_id=task_id,
                state="NEEDS_ATTENTION",
                reason="workflow cannot resume because required tools are unavailable: " + ", ".join(missing_required),
            )
            self.store.upsert_task(
                task_id=task_id,
                principal=principal,
                definition_id=definition.id,
                envelope=envelope,
                context=row.get("context") or {},
                data=row.get("data") or {},
                trace=row.get("trace") or [],
            )
            return envelope
        context = dict(row.get("context") or {})
        data = row.get("data") or {}
        results = data.setdefault("results", {})
        qid = row.get("question_id") or question_id or ""
        data.setdefault("resume", {})[qid] = answer
        results["user_answer"] = {"question_id": qid, "answer": answer, "source": answer_source or "unknown"}
        if input_reference is not None:
            results["resume_input_reference"] = {"question_id": qid, "input": input_reference}
        if answer_source is not None:
            context["user_text_source"] = answer_source
            data.setdefault("context", {})["user_text_source"] = answer_source
        resume_next = row.get("resume_next") or self._resume_next_from_wait_step(definition, data, qid)
        if not resume_next:
            envelope = WorkflowEnvelope(
                task_id=task_id,
                state="NEEDS_ATTENTION",
                reason="workflow cannot resume because the waiting step has no next step",
            )
            self.store.upsert_task(
                task_id=task_id,
                principal=principal,
                definition_id=definition.id,
                envelope=envelope,
                context=context,
                data=data,
                trace=row.get("trace") or [],
            )
            return envelope
        executor = WorkflowExecutor(
            definition=definition,
            tools=registry,
            llm=WorkflowLLMAdapter(provider),
            task_id=task_id,
            context_snapshot=context,
            data=data,
            trace=row.get("trace") or [],
            start_step=resume_next,
        )
        coro = self._execute_and_persist(executor, principal, definition, context)
        if self.sync_wait_seconds <= 0:
            running = WorkflowEnvelope(task_id=task_id, state="RUNNING", reason="workflow resume execution started")
            self.store.set_lease(task_id, lease_until=time.time() + _LEASE_SECONDS)
            task = asyncio.create_task(coro)
            self._tasks[task_id] = task
            task.add_done_callback(lambda t, task_id=task_id: self._tasks.pop(task_id, None))
            self.store.upsert_task(
                task_id=task_id,
                principal=principal,
                definition_id=definition.id,
                envelope=running,
                context=context,
                data=data,
                trace=row.get("trace") or [],
            )
            return running
        try:
            return await asyncio.wait_for(asyncio.shield(coro), timeout=self.sync_wait_seconds)
        except asyncio.TimeoutError:
            running = WorkflowEnvelope(task_id=task_id, state="RUNNING", reason="workflow resume execution started")
            self.store.set_lease(task_id, lease_until=time.time() + _LEASE_SECONDS)
            task = asyncio.create_task(coro)
            self._tasks[task_id] = task
            task.add_done_callback(lambda t, task_id=task_id: self._tasks.pop(task_id, None))
            self.store.upsert_task(
                task_id=task_id,
                principal=principal,
                definition_id=definition.id,
                envelope=running,
                context=context,
                data=data,
                trace=row.get("trace") or [],
            )
            return running

    def cancel(self, *, principal: WorkflowPrincipal, task_id: str, reason: str | None = None) -> WorkflowEnvelope:
        row = self.store.get_task(task_id)
        if row is None:
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow task is not available")
        try:
            ensure_task_access(principal, row)
        except WorkflowAccessDenied:
            return WorkflowEnvelope(task_id=task_id, state="NEEDS_ATTENTION", reason="workflow task is not available")
        envelope = WorkflowEnvelope(task_id=task_id, state="CANCELLED", reason=reason or "workflow task cancelled")
        self.store.upsert_task(
            task_id=task_id,
            principal=principal,
            definition_id=row.get("definition_id") or "situation_judgment.v1",
            envelope=envelope,
            context=row.get("context") or {},
            data=row.get("data") or {},
            trace=row.get("trace") or [],
        )
        return envelope

    async def deliver_background(self, task_id: str) -> None:
        row = self.store.get_task(task_id)
        if not row or not self.bus:
            return
        envelope = WorkflowEnvelope.model_validate(row["envelope"])
        principal = WorkflowPrincipal.model_validate(row["principal"])
        content = envelope.reason if envelope.state == "COMPLETED" else envelope.question
        if not content or not principal.channel or not principal.chat_id:
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel=principal.channel,
            chat_id=principal.chat_id,
            content=content,
            metadata={"workflow_task_id": task_id},
        ))
        self.store.mark_delivered(task_id, handed_to_channel=True)

    def _same_session_summary(self, principal: WorkflowPrincipal) -> list[dict[str, Any]]:
        return [self._summary(row) for row in self.store.list_tasks(principal, states={"RUNNING", "WAITING_USER"})]

    @staticmethod
    def _tool_still_required_after_resume(definition: WorkflowDefinition, tool_name: str) -> bool:
        for step in definition.steps:
            branches = step.config.get("branches")
            if not isinstance(branches, dict):
                continue
            for branch in branches.values():
                if not isinstance(branch, dict):
                    continue
                if branch.get("tool") == tool_name and bool(branch.get("required", True)):
                    return True
        return False

    @staticmethod
    def _resume_next_from_wait_step(definition: WorkflowDefinition, data: dict[str, Any], question_id: str) -> str | None:
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            return None
        candidates: list[str] = []
        for step in definition.steps:
            if step.type != "wait_user":
                continue
            output_key = step.output or step.id
            stored = results.get(output_key)
            if isinstance(stored, dict) and question_id and stored.get("question_id") == question_id:
                return step.next
            if step.next:
                candidates.append(step.next)
        if not question_id and len(candidates) == 1:
            return candidates[0]
        return None

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": row.get("task_id"),
            "state": row.get("state"),
            "delivery_state": row.get("delivery_state"),
            "question_id": row.get("question_id"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _load_definition(definition_id: str) -> WorkflowDefinition:
        if definition_id != "situation_judgment.v1":
            raise ValueError(f"unknown workflow definition: {definition_id}")
        path = Path(__file__).resolve().parent / "definitions" / "situation_judgment.v1.json"
        return WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))

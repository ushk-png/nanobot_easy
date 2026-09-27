"""Minimal workflow executor for stage 2.

Implements the five stage types from the design: llm, tool, branch, wait_user,
and end. Persistence, lease/recovery, delivery hooks, and runtime loop
connections are intentionally left for later approved stages.
"""
from __future__ import annotations

import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result
from nanobot.workflow.conditions import ConditionError, evaluate_condition, resolve_path
from nanobot.workflow.llm_adapter import WorkflowLLMAdapter, WorkflowLLMError
from nanobot.workflow.schema import WorkflowDefinition, WorkflowEnvelope, WorkflowStep
from nanobot.workflow.validators import run_validators
from nanobot.workflow.validator import validate_definition

_TERMINAL_STATES = {"COMPLETED", "WAITING_USER", "FAILED", "NEEDS_ATTENTION", "CANCELLED"}


@dataclass(slots=True)
class WorkflowExecutionResult:
    envelope: WorkflowEnvelope
    data: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)


class WorkflowExecutionError(RuntimeError):
    pass


class WorkflowExecutor:
    def __init__(
        self,
        *,
        definition: WorkflowDefinition,
        tools: ToolRegistry,
        llm: WorkflowLLMAdapter,
        task_id: str | None = None,
        context_snapshot: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        trace: list[dict[str, Any]] | None = None,
        start_step: str | None = None,
    ) -> None:
        self.definition = definition
        self.tools = tools
        self.llm = llm
        self.task_id = task_id or f"wf_{uuid.uuid4().hex[:12]}"
        self.data: dict[str, Any] = deepcopy(data) if data is not None else {
            "context": deepcopy(context_snapshot or {}),
            "results": {},
            "available_actions": [],
            "tool_observations": [],
        }
        if context_snapshot is not None:
            self.data["context"] = deepcopy(context_snapshot)
        self.data.setdefault("results", {})
        self.data.setdefault("available_actions", [])
        self.data.setdefault("tool_observations", [])
        self.trace: list[dict[str, Any]] = deepcopy(trace or [])
        self.start_step = start_step or definition.start
        self._steps = {step.id: step for step in definition.steps}
        self._step_count = 0
        self._llm_calls = 0
        self._tool_calls = 0
        self._started = time.monotonic()

    def prune_optional_branches(self) -> set[str]:
        """Disable optional branches whose required tool is unavailable.

        Branch config supports:
        {"branches": {"action": {"next": "step", "tool": "web_search", "required": false}}}
        """

        available: set[str] = set()
        for step in self.definition.steps:
            if step.type != "branch":
                continue
            branches = step.config.get("branches")
            if not isinstance(branches, dict):
                continue
            for action, branch in branches.items():
                if not isinstance(branch, dict):
                    available.add(str(action))
                    continue
                tool_name = branch.get("tool")
                required = bool(branch.get("required", True))
                if tool_name and not self.tools.has(str(tool_name)):
                    if required:
                        continue
                    branch["pruned"] = True
                    continue
                available.add(str(action))
        self.data["available_actions"] = sorted(available)
        return available

    async def run(self) -> WorkflowExecutionResult:
        validation = validate_definition(self.definition, available_tools=set(self.tools.tool_names))
        if not validation.ok:
            return self._finish("NEEDS_ATTENTION", reason="; ".join(validation.errors))
        self.prune_optional_branches()
        current = self.start_step
        while True:
            self._enforce_budget()
            step = self._steps[current]
            self._step_count += 1
            self.trace.append({"step": step.id, "type": step.type})
            if step.type == "llm":
                current = await self._run_llm(step)
                continue
            if step.type == "tool":
                current = await self._run_tool(step)
                continue
            if step.type == "branch":
                current = self._run_branch(step)
                continue
            if step.type == "wait_user":
                return self._run_wait_user(step)
            if step.type == "end":
                return self._run_end(step)
            return self._finish("FAILED", reason=f"unsupported step type: {step.type}")

    def _enforce_budget(self) -> None:
        budget = self.definition.budget
        if self._step_count >= budget.max_steps:
            raise WorkflowExecutionError("workflow max_steps exceeded")
        if self._llm_calls > budget.max_llm_calls:
            raise WorkflowExecutionError("workflow max_llm_calls exceeded")
        if self._tool_calls > budget.max_tool_calls:
            raise WorkflowExecutionError("workflow max_tool_calls exceeded")
        if time.monotonic() - self._started > budget.wall_time_seconds:
            raise WorkflowExecutionError("workflow wall_time_seconds exceeded")

    async def _run_llm(self, step: WorkflowStep) -> str:
        self._llm_calls += 1
        prompt = str(step.config.get("prompt") or step.id)
        payload = {
            "workflow_id": self.definition.id,
            "step_id": step.id,
            "prompt": prompt,
            "inputs": self._resolve_inputs(step.inputs),
            "data": self.data,
            "available_actions": self.data.get("available_actions", []),
        }
        try:
            result = await self.llm.call_json(system=prompt, payload=payload)
        except WorkflowLLMError as exc:
            self._store(step, {"error": str(exc)})
            return step.on_error or self._fail_step(step, str(exc))
        validator_names = [str(v) for v in step.config.get("validators") or []]
        errors = run_validators(validator_names, result, step.config.get("validator_config") or {})
        if errors:
            self._store(step, {"error": "; ".join(errors), "raw": result})
            return step.on_error or self._fail_step(step, "; ".join(errors))
        self._store(step, result)
        return step.next or self._fail_step(step, "llm step has no next")

    async def _run_tool(self, step: WorkflowStep) -> str:
        self._tool_calls += 1
        tool_name = str(step.config.get("tool") or "")
        if tool_name in {"workflow", "spawn", "delegate"}:
            return self._fail_step(step, f"workflow may not call tool {tool_name!r}")
        params = self._resolve_inputs(step.inputs)
        for key, value in (step.config.get("params") or {}).items():
            params[key] = self._resolve_value(value)
        tool, cast_params, error = self.tools.prepare_call(tool_name, params)
        if error is not None:
            self._store(step, {"error": str(error)})
            return step.on_error or self._fail_step(step, str(error))
        assert tool is not None
        try:
            result = await tool.execute(**cast_params)
        except Exception as exc:
            self._store(step, {"error": str(exc)})
            return step.on_error or self._fail_step(step, str(exc))
        if is_tool_error_result(tool_name, result):
            self._store(step, {"error": str(result)})
            return step.on_error or self._fail_step(step, str(result))
        self._store(step, result)
        self.data["tool_observations"].append({"tool": tool_name, "result": result})
        return step.next or self._fail_step(step, "tool step has no next")

    def _run_branch(self, step: WorkflowStep) -> str:
        branches = step.config.get("branches") or {}
        value = self._resolve_value(step.config.get("value", {"ref": "results.judge_sufficiency.action"}))
        default_next = step.config.get("default") or step.on_error
        branch = branches.get(value)
        if isinstance(branch, dict) and branch.get("pruned"):
            branch = None
        if isinstance(branch, str):
            return branch
        if isinstance(branch, dict):
            condition = branch.get("condition")
            if isinstance(condition, dict):
                try:
                    if not evaluate_condition(condition, self.data):
                        return str(default_next or self._fail_step(step, f"branch condition failed: {value}"))
                except ConditionError as exc:
                    return str(default_next or self._fail_step(step, str(exc)))
            next_step = branch.get("next")
            if isinstance(next_step, str):
                return next_step
        return str(default_next or self._fail_step(step, f"no branch for value: {value!r}"))

    def _run_wait_user(self, step: WorkflowStep) -> WorkflowExecutionResult:
        question = self._resolve_value(step.config.get("question", {"ref": "results.judge_sufficiency.question"}))
        if not isinstance(question, str) or not question.strip():
            question = "추가 정보가 필요합니다. 답변해 주세요."
        question_id = f"q_{uuid.uuid4().hex[:12]}"
        self._store(step, {"question": question, "question_id": question_id, "resume_next": step.next})
        return self._finish("WAITING_USER", deliver="question", question=question, question_id=question_id)

    def _run_end(self, step: WorkflowStep) -> WorkflowExecutionResult:
        state = str(step.config.get("state") or "COMPLETED")
        if state not in _TERMINAL_STATES:
            state = "FAILED"
        answer = self._resolve_value(step.config.get("answer", {"ref": "results.draft_answer.answer"}))
        reason = self._resolve_value(step.config.get("reason")) if "reason" in step.config else None
        if state == "COMPLETED":
            return self._finish(state, deliver="final", reason=str(answer or ""))
        return self._finish(state, reason=str(reason or answer or ""))

    def _store(self, step: WorkflowStep, value: Any) -> None:
        key = step.output or step.id
        self.data.setdefault("results", {})[key] = value

    def _resolve_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {key: self._resolve_value(value) for key, value in inputs.items()}

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, dict) and set(value.keys()) == {"ref"}:
            try:
                return resolve_path(self.data, str(value["ref"]))
            except ConditionError:
                return None
        if isinstance(value, list):
            return [self._resolve_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve_value(v) for k, v in value.items()}
        return value

    def _fail_step(self, step: WorkflowStep, reason: str) -> str:
        self.data.setdefault("results", {})["__failure__"] = {"step": step.id, "reason": reason}
        fail_id = "__workflow_failed__"
        if fail_id not in self._steps:
            self._steps[fail_id] = WorkflowStep(
                id=fail_id,
                type="end",
                config={"state": "FAILED", "reason": {"ref": "results.__failure__.reason"}},
            )
        return fail_id

    def _finish(
        self,
        state: str,
        *,
        deliver: str = "none",
        question: str | None = None,
        question_id: str | None = None,
        reason: str | None = None,
    ) -> WorkflowExecutionResult:
        envelope = WorkflowEnvelope(
            task_id=self.task_id,
            state=state,  # type: ignore[arg-type]
            delivery_state="pending",
            deliver=deliver,  # type: ignore[arg-type]
            question=question,
            question_id=question_id,
            reason=reason,
        )
        return WorkflowExecutionResult(envelope=envelope, data=self.data, trace=self.trace)

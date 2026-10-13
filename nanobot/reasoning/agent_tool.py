"""Turn-local opt-in assistance, selected by the normal Agent's first call.

No shell, external tools, generated Python or new execution permissions are exposed.
"""
from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.reasoning.bridge import (
    ProviderStageLLM,
    _review_procedure,
    is_explicit_continuation,
    prepare_dynamic_procedure,
    recent_conversation,
    run_reasoning_procedure,
    user_reply,
)
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import BudgetExceededError, RunBudget


class ReasoningAssistTool(Tool):
    name = "reasoning_assist"
    description = (
        "Optional bounded code assistance. Answer ordinary questions directly and use existing "
        "tools for real investigation/execution. Do not classify every request. "
        "calculate applies an allowlisted numeric operation to confirmed, same-unit values. "
        "workflow is exceptional: supply actual intermediate-result dependencies, not generic "
        "evaluation stages. Supply target/action/deliverable/requirements/completion in contract. "
        "No external execution permission is granted. Return the tool's core results faithfully; "
        "unknown data is not confirmed. read returns the saved result without computation for "
        "rephrasing or explanation; resume continues an unfinished saved task; update_facts "
        "accepts explicit corrections/new evidence; update_contract patches named criteria or goal "
        "within the same task only. Read first and pass the saved expected_revision for mutations. "
        "Never turn a new topic into an update or repeat external operations through this tool."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["calculate", "workflow", "read", "resume", "update_facts", "update_contract"]},
            "contract": {"type": "object", "properties": {
                key: {"type": "string", "minLength": 1}
                for key in ("target", "action", "deliverable", "requirements", "completion")
            }, "required": ["target", "action", "deliverable", "requirements", "completion"]},
            "operation": {"type": "string", "enum": ["sum", "min", "max", "mean", "count"]},
            "values": {"type": "array", "maxItems": 256, "items": {"type": "number"}},
            "data_status": {"type": "string", "enum": ["confirmed", "estimated", "unknown"]},
            "unit": {"type": "string"},
            "procedure": {"type": "object"},
            "first_step_output": {"type": "object"},
            "expected_revision": {"type": "integer", "minimum": 1},
            "facts": {"type": "array", "maxItems": 256, "items": {"type": "object"}},
            "criteria": {"type": "array", "maxItems": 256, "items": {"type": "object"}},
            "goal": {"type": "string", "minLength": 1},
        },
        "required": ["mode", "contract"],
    }

    def __init__(self, loop: Any, ctx: Any, started: float) -> None:
        self.loop, self.ctx, self.started = loop, ctx, started
        self.used = False

    async def execute(self, mode: str, contract: dict, operation: str = "count",
                      values: list | None = None, data_status: str = "unknown",
                      unit: str = "", procedure: dict | None = None,
                      first_step_output: dict | None = None,
                      expected_revision: int | None = None,
                      facts: list | None = None, criteria: list | None = None,
                      goal: str | None = None) -> str:
        # Includes prior general-loop calls and elapsed time; never reset at transition.
        budget = RunBudget(started=self.started, calls=max(1, self.loop._current_iteration))
        if mode in {"read", "resume", "update_facts", "update_contract"}:
            return await self._followup(mode, contract, budget, expected_revision, facts, criteria, goal)
        if self.used or not budget.can_spend(0):
            return ToolResult.error("Assistance already used or turn budget exhausted; report partial result.")
        self.used = True
        if mode == "calculate":
            if data_status != "confirmed":
                return ToolResult.error("Unconfirmed input: obtain evidence before calculating a definitive result.")
            numbers = values or []
            if not numbers or len(numbers) > 256 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in numbers
            ):
                return ToolResult.error("Expected 1–256 finite numeric inputs with a common unit.")
            operations = {"sum": lambda: sum(numbers), "min": lambda: min(numbers),
                          "max": lambda: max(numbers), "mean": lambda: sum(numbers) / len(numbers),
                          "count": lambda: len(numbers)}
            if operation not in operations:
                return ToolResult.error("Unsupported operation.")
            result = operations[operation]()
            if not math.isfinite(result):
                return ToolResult.error("Calculation overflow; no valid result.")
            return json.dumps({"status": "completed", "operation": operation,
                               "result": result, "unit": unit, "contract": contract}, ensure_ascii=False)
        if mode != "workflow" or not procedure:
            return ToolResult.error("Workflow requires an explicit dependency definition.")
        selected, _, defects = prepare_dynamic_procedure(
            procedure, request=self.ctx.msg.content, known_goal="", max_steps=4,
        )
        if selected is None:
            return ToolResult.error("Invalid workflow: " + "; ".join(d.message() for d in defects))
        if len(selected.steps) < 2 or not any(
            ref in {previous.id for previous in selected.steps[:index]}
            for index, step in enumerate(selected.steps) for ref in step.inputs
        ):
            return ToolResult.error("Workflow needs a real prior-result dependency; answer in the normal loop.")
        conversation = recent_conversation(self.ctx.initial_messages)
        reviewer = ProviderStageLLM(self.loop.provider, self.loop.model)
        reviewer.budget = budget
        try:
            review_defects, _ = await _review_procedure(
                reviewer, self.ctx.msg.content, "", selected, conversation,
            )
        except BudgetExceededError:
            return ToolResult.error("Workflow review budget exhausted; no workflow executed.")
        if review_defects:
            return ToolResult.error("Workflow fitness review failed: " + "; ".join(review_defects))
        case = await run_reasoning_procedure(
            self.loop.provider, model=self.loop.model, request=self.ctx.msg.content,
            workspace=self.loop.workspace, sessions=self.loop._stepwise_sessions(),
            session_key=self.ctx.session_key, conversation=conversation,
            supplied_procedure=selected, extracted=first_step_output, shared_budget=budget,
            fallback_direct=False,
        )
        return json.dumps({"status": case.status, "answer": user_reply(case),
                           "contract": contract, "unresolved": case.stop_reason}, ensure_ascii=False)

    async def _followup(self, mode: str, contract: dict, budget: RunBudget,
                        expected_revision: int | None, facts: list | None,
                        criteria: list | None, goal: str | None) -> str:
        sessions = self.loop._stepwise_sessions()
        prior = sessions.get(self.ctx.session_key)
        if prior is None:
            return ToolResult.error("No saved task. Use the normal loop; do not invent prior state.")
        original, procedure = prior
        if mode == "read":
            return json.dumps({"task_id": original.id, "revision": original.execution_revision,
                               "request": original.request, "status": original.status,
                               "answer": original.answer, "goal": original.goal,
                               "facts": [f.model_dump() for f in original.facts],
                               "criteria": [c.model_dump() for c in original.criteria],
                               "questions": original.questions, "unresolved": original.stop_reason},
                              ensure_ascii=False)
        if self.used or not budget.can_spend(0):
            return ToolResult.error("Follow-up budget exhausted or already used.")
        if expected_revision != original.execution_revision:
            return ToolResult.error("Stale or missing revision. Read current saved task before updating.")
        if mode == "resume" and original.status == "incomplete" and not is_explicit_continuation(self.ctx.msg.content):
            return ToolResult.error("Incomplete work requires an explicit continuation, not a new topic.")
        if mode == "resume" and original.status not in {"incomplete", "waiting_info"}:
            return ToolResult.error("Only unfinished or waiting tasks can resume.")
        if mode != "resume" and original.status != "completed":
            return ToolResult.error("Resolve the unfinished task before changing its facts or contract.")
        # Detach all mutable outputs: cancellation/late results cannot mutate stored state.
        snapshot = original.model_dump_json()
        case = original.model_copy(deep=True)
        self.used = True
        llm = ProviderStageLLM(self.loop.provider, self.loop.model)
        llm.budget = budget
        engine = ReasoningEngine(llm)
        try:
            if mode == "resume":
                result = await asyncio.to_thread(engine.resume, case, procedure, self.ctx.msg.content)
            elif mode == "update_facts":
                if not any("facts" in step.roles for step in procedure.steps) or not any(
                    "judgments" in step.roles for step in procedure.steps
                ):
                    return ToolResult.error("This workflow has no typed fact/judgment update boundary; use a reviewed replacement.")
                if not facts or any(not isinstance(f, dict) or not f.get("id") or
                                    not f.get("proposition") for f in facts):
                    return ToolResult.error("Explicit fact IDs and propositions required; read prior facts first.")
                normalized = [{**f, "source": "user", "status": f.get("status") or "unknown"} for f in facts]
                result = await asyncio.to_thread(engine.continue_with_facts, case, procedure, normalized)
            else:
                if not any("criteria" in step.roles for step in procedure.steps) or not any(
                    "judgments" in step.roles for step in procedure.steps
                ):
                    return ToolResult.error("This workflow has no typed criterion/judgment update boundary; use a reviewed replacement.")
                if not criteria and not goal:
                    return ToolResult.error("Specify a changed goal or named criterion patches.")
                known = {c.id: c for c in case.criteria}
                if any(not isinstance(c, dict) or c.get("id") not in known for c in criteria or []):
                    return ToolResult.error("Criterion patch must identify an existing criterion; new criteria require a reviewed workflow.")
                patches = [{**known[c["id"]].model_dump(), **c} for c in criteria or []]
                result = await asyncio.to_thread(engine.continue_with_contract, case, procedure, patches, goal)
        except (ValueError, KeyError, BudgetExceededError) as exc:
            return ToolResult.error(f"Follow-up not applied: {exc}")
        current = sessions.get(self.ctx.session_key)
        if current is None or current[0].model_dump_json() != snapshot:
            return ToolResult.error("Saved task changed or expired during execution; late result discarded.")
        result.execution_revision = original.execution_revision + 1
        result.history.append({"event": "followup_committed", "mode": mode,
                               "revision": result.execution_revision})
        sessions.put(self.ctx.session_key, result, procedure)
        return json.dumps({"task_id": result.id, "revision": result.execution_revision,
                           "status": result.status, "answer": user_reply(result),
                           "contract": contract, "unresolved": result.stop_reason}, ensure_ascii=False)

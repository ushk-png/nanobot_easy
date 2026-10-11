"""Execute the method-evaluation procedure under code control."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from nanobot.reasoning.checks import ANSWER_MISSING, check_stage
from nanobot.reasoning.llm import StageLLM
from nanobot.reasoning.models import (
    CheckRecord,
    Conclusion,
    Criterion,
    Fact,
    Judgment,
    Procedure,
    ReasoningCase,
    Usage,
)
from nanobot.reasoning.registry import procedure as load_procedure
from nanobot.reasoning.z3check import check_entailment

_ROLE_CHECKS = {
    "goal": "S1",
    "facts": "S2",
    "criteria": "S3",
    "judgments": "S4",
    "conclusion": "S5",
    "check": "S6",
    "answer": "S7",
}


class ReasoningEngine:
    def __init__(
        self,
        llm: StageLLM,
        *,
        reasoning_effort: str | None = "none",
        reasoning_disabled_guaranteed: bool = False,
        max_revisions: int = 3,
        max_stage_calls: int = 64,
        on_stage: Callable[[str, str, str], None] | None = None,
        z3_check: bool = False,
    ) -> None:
        self.llm = llm
        self.reasoning_effort = reasoning_effort
        self.reasoning_disabled_guaranteed = reasoning_disabled_guaranteed
        self.max_revisions = max_revisions
        self.max_stage_calls = max_stage_calls
        self.on_stage = on_stage
        # Off until rules are actually formalized; a disabled check is recorded, never passed.
        self.z3_check = z3_check
        self._procedure: Procedure | None = None

    def run(
        self,
        request: str,
        *,
        procedure_id: str = "method-evaluation",
        procedure: Procedure | None = None,
        case_id: str | None = None,
        conversation: list[dict[str, str]] | None = None,
    ) -> ReasoningCase:
        procedure = procedure or load_procedure(procedure_id)
        self._procedure = procedure
        case = ReasoningCase(
            id=case_id or f"reason-{uuid4().hex[:12]}",
            request=request,
            procedure_id=procedure.id,
            procedure_version=procedure.version,
            step_states={step.id: "pending" for step in procedure.steps},
            conversation=list(conversation or []),
            execution_revision=procedure.revision,
            usage=Usage(
                reasoning_effort=self.reasoning_effort,
                reasoning_disabled_guaranteed=self.reasoning_disabled_guaranteed,
            ),
        )
        return self._execute(case, procedure)

    def resume(self, case: ReasoningCase, procedure: Procedure, reply: str) -> ReasoningCase:
        """Continue a case that waited for information, from the waiting step (design 6.2, 7)."""

        self._procedure = procedure
        waiting = [item for item, state in case.step_states.items() if state == "waiting_info"]
        start = waiting[0] if waiting else self._first_role("goal")
        asked = " / ".join(case.questions)
        case.request = f"{case.request}\n\n[질문] {asked}\n[사용자 답변] {reply.strip()}"
        case.questions = []
        self._invalidate_from(case, start)
        case.revision_count = 0
        case.history.append({"event": "resumed", "from": start})
        return self._execute(case, procedure)

    def continue_with_facts(
        self,
        case: ReasoningCase,
        procedure: Procedure,
        facts: list[dict[str, Any]],
    ) -> ReasoningCase:
        """Apply new or changed facts and re-run only the dependent steps (design 7, 13)."""

        self.update_facts(case, facts, procedure)
        case.revision_count = 0
        return self._execute(case, procedure)

    def _execute(self, case: ReasoningCase, procedure: Procedure) -> ReasoningCase:
        self._procedure = procedure
        case.status = "running"
        order = self._execution_order(procedure)
        index = 0
        stage_calls = 0
        step_calls: dict[str, int] = {}
        repair: dict[str, list[str]] = {}
        while index < len(order):
            stage = order[index]
            if case.step_states.get(stage) in {"passed", "conditionally_passed"}:
                # Results whose inputs did not change are reused (design 7, 12).
                index += 1
                continue
            if stage_calls >= self.max_stage_calls:
                self._stop(case, stage, "stage call budget exceeded")
                break
            stage_calls += 1
            if case.revision_count > self.max_revisions:
                self._stop(case, stage, "revision budget exceeded")
                break
            if not self._inputs_ready(procedure, stage, case):
                self._stop(case, stage, f"{stage} inputs are not ready")
                break
            case.current_step = stage
            case.step_states[stage] = "running"
            step_calls[stage] = step_calls.get(stage, 0) + 1
            payload = self.llm.complete(stage, self._payload(case, stage, repair.pop(stage, None)))
            if _result_revision(payload, procedure.revision) != case.execution_revision:
                case.history.append({"event": "stale_result_ignored", "stage": stage})
                stale_count = sum(
                    1
                    for item in case.history
                    if item.get("event") == "stale_result_ignored" and item.get("stage") == stage
                )
                if stale_count > 1:
                    self._stop(case, stage, "stale revision result repeated")
                    break
                continue
            self._record_usage(case, payload)
            shape_defects = self._apply(case, stage, payload)
            if self._step(procedure, stage).role == "check" and case.procedure_id == "rule-judgment":
                if self.z3_check:
                    self._check_rule_entailment(case)
                else:
                    self._record_z3_disabled(case)
            defects = shape_defects + check_stage(
                case,
                self._check_stage(procedure, stage),
                declared_outputs=self._step(procedure, stage).outputs,
                result=payload,
            )
            if defects:
                case.step_states[stage] = "needs_revision"
                step = self._step(procedure, stage)
                if self._retry_same_step(step, defects, step_calls.get(stage, 0), case):
                    # Re-ask only this step with the defects (design 4.5).
                    case.revision_count += 1
                    repair[stage] = defects
                    case.history.append({"event": "retry", "stage": stage, "defects": defects})
                    continue
                if ANSWER_MISSING in defects and _is_answer_step(step):
                    self._stop(case, stage, ANSWER_MISSING)
                    case.status = "failed_empty_answer"
                    break
                target = self._defect_target(procedure, order, stage, payload)
                if not self._return_to(case, stage, target):
                    self._stop(case, stage, "; ".join(defects))
                    break
                index = order.index(case.current_step)
                continue
            case.step_states[stage] = "passed"
            self._notify_stage(stage, "passed")
            if self._step(procedure, stage).role == "goal" and case.questions:
                case.status = "waiting_info"
                case.step_states[stage] = "waiting_info"
                case.stop_reason = "\n".join(case.questions)
                break
            index += 1
        else:
            if case.answer.strip():
                case.status = "completed"
                case.stop_reason = "procedure completed"
            else:
                # Every step passed but nothing filled the answer field (design 11).
                case.status = "failed_empty_answer"
                case.stop_reason = ANSWER_MISSING
        return case

    def _retry_same_step(self, step, defects: list[str], calls: int, case: ReasoningCase) -> bool:
        if calls >= step.budget.max_calls or case.revision_count >= self.max_revisions:
            return False
        # A step's own malformed or missing output is fixed at that step; judgment
        # defects use failure_returns_to.
        return (
            step.failure_returns_to is None
            or defects == [ANSWER_MISSING]
            or any(_is_format_defect(item) for item in defects)
        )

    def update_facts(
        self,
        case: ReasoningCase,
        facts: list[dict[str, Any]],
        procedure: Procedure | None = None,
    ) -> ReasoningCase:
        """Replace changed facts and invalidate only dependent judgments."""

        if procedure is not None:
            self._procedure = procedure
        elif self._procedure is None or self._procedure.id != case.procedure_id:
            self._procedure = load_procedure(case.procedure_id)
        by_id = {item.id: item for item in case.facts}
        changed: set[str] = set()
        for raw in facts:
            fact = Fact.model_validate(raw)
            previous = by_id.get(fact.id)
            fact.version = 1 if previous is None else previous.version + 1
            by_id[fact.id] = fact
            changed.add(fact.id)
        case.facts = list(by_id.values())
        if changed:
            case.judgments = [
                item for item in case.judgments if not set(item.evidence_ids) & changed
            ]
            case.conclusion = None
            case.answer = ""
            self._invalidate_from(case, self._first_role("judgments"))
            case.current_step = self._first_role("judgments")
            case.status = "running"
            case.history.append({"event": "facts_updated", "fact_ids": sorted(changed)})
        return case

    def _apply(self, case: ReasoningCase, stage: str, payload: dict[str, Any]) -> list[str]:
        """Store a stage result. Malformed typed output is a step defect, not a crash."""

        role = self._step(self._require_procedure(), stage).role
        defects: list[str] = []
        try:
            if role == "goal":
                case.goal = str(payload.get("goal") or "")
                case.constraints = [str(item) for item in payload.get("constraints") or []]
                case.questions = [str(item) for item in payload.get("questions") or []]
                case.assumptions = [str(item) for item in payload.get("assumptions") or []]
            elif role == "facts":
                case.facts = [Fact.model_validate(item) for item in _as_list(payload.get("facts"))]
            elif role == "criteria":
                case.criteria = [
                    Criterion.model_validate(item) for item in _as_list(payload.get("criteria"))
                ]
            elif role == "judgments":
                case.judgments = [
                    Judgment.model_validate(item) for item in _as_list(payload.get("judgments"))
                ]
            elif role == "conclusion":
                raw = payload.get("conclusion")
                case.conclusion = Conclusion.model_validate(raw) if isinstance(raw, dict) else None
        except ValidationError:
            defects.append(f"{role} output has an invalid shape")
        step = self._step(self._require_procedure(), stage)
        case.step_outputs[stage] = {
            key: payload[key] for key in step.outputs if key in payload and key != "usage"
        }
        if _is_answer_step(step):
            answer = payload.get("answer")
            case.answer = answer if isinstance(answer, str) else ""
        case.history.append({"event": "stage_result", "stage": stage, "role": role})
        return defects

    def _defect_target(
        self,
        procedure: Procedure,
        order: list[str],
        stage: str,
        payload: dict[str, Any],
    ) -> str | None:
        """Check steps name where each defect is fixed; code keeps only earlier steps (design 5, 9)."""

        if self._step(procedure, stage).role != "check":
            return None
        position = order.index(stage)
        targets = [
            str(item.get("step") or "").strip()
            for item in payload.get("defects") or []
            if isinstance(item, dict)
        ]
        valid = [item for item in targets if item in order[:position]]
        return min(valid, key=order.index) if valid else None

    def _return_to(self, case: ReasoningCase, stage: str, target: str | None = None) -> bool:
        if self._procedure is None:
            raise RuntimeError("procedure is not loaded")
        target = target or self._procedure.step(stage).failure_returns_to
        if target is None or case.revision_count >= self.max_revisions:
            return False
        case.revision_count += 1
        self._invalidate_from(case, target if target != stage else stage)
        case.current_step = target
        case.history.append({"event": "return", "from": stage, "to": target})
        return True

    def _invalidate_from(self, case: ReasoningCase, stage: str) -> None:
        procedure = self._require_procedure()
        affected = self._downstream(stage)
        for item in affected:
            case.step_states[item] = "invalidated"
        # An invalidated step's result must not survive as the current result.
        steps = [procedure.step(item) for item in affected]
        if any(step.role == "conclusion" for step in steps):
            case.conclusion = None
        if any(_is_answer_step(step) for step in steps):
            case.answer = ""

    def _execution_order(self, procedure: Procedure) -> list[str]:
        known = {step.id for step in procedure.steps}
        dependencies = {
            step.id: [item for item in step.inputs if item in known]
            for step in procedure.steps
        }
        ordered: list[str] = []
        pending = dict(dependencies)
        while pending:
            ready = [step_id for step_id, deps in pending.items() if all(item in ordered for item in deps)]
            if not ready:
                return [step.id for step in procedure.steps]
            ordered.extend(sorted(ready, key=lambda item: [step.id for step in procedure.steps].index(item)))
            for step_id in ready:
                pending.pop(step_id)
        return ordered

    def _downstream(self, stage: str) -> set[str]:
        procedure = self._require_procedure()
        dependents = {stage}
        changed = True
        while changed:
            changed = False
            for step in procedure.steps:
                if step.id not in dependents and (stage in step.inputs or dependents & set(step.inputs)):
                    dependents.add(step.id)
                    changed = True
        return dependents

    def _inputs_ready(self, procedure: Procedure, stage: str, case: ReasoningCase) -> bool:
        known = {step.id for step in procedure.steps}
        for ref in self._step(procedure, stage).inputs:
            if ref in known and case.step_states.get(ref) not in {"passed", "conditionally_passed"}:
                return False
        return True

    def _check_stage(self, procedure: Procedure, stage: str) -> str:
        step = self._step(procedure, stage)
        if _is_answer_step(step):
            return _ROLE_CHECKS["answer"]
        return _ROLE_CHECKS.get(step.role, "")

    def _first_role(self, role: str) -> str:
        procedure = self._require_procedure()
        for step in procedure.steps:
            if step.role == role:
                return step.id
        return procedure.steps[0].id

    def _step(self, procedure: Procedure, stage: str):
        return procedure.step(stage)

    def _require_procedure(self) -> Procedure:
        if self._procedure is None:
            raise RuntimeError("procedure is not loaded")
        return self._procedure

    def _notify_stage(self, stage: str, state: str) -> None:
        if self.on_stage is None or self._procedure is None:
            return
        step = self._step(self._procedure, stage)
        label = (step.question or step.role or stage).strip().splitlines()[0][:80]
        self.on_stage(stage, state, label)

    def _stop(self, case: ReasoningCase, stage: str, reason: str) -> None:
        case.status = "stopped"
        case.step_states[stage] = "stopped"
        case.stop_reason = reason
        self._notify_stage(stage, "stopped")

    def _record_usage(self, case: ReasoningCase, payload: dict[str, Any]) -> None:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        case.usage.calls += 1
        case.usage.input_tokens += int(usage.get("input_tokens") or 0)
        case.usage.output_tokens += int(usage.get("output_tokens") or 0)
        case.usage.reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
        case.usage.duration_ms += int(usage.get("duration_ms") or 0)

    def _record_z3_disabled(self, case: ReasoningCase) -> None:
        case.checks.append(
            CheckRecord(
                id=f"check-{len(case.checks) + 1}",
                target_version=f"{case.procedure_id}:{case.procedure_version}",
                kind="z3:entailment",
                result="DISABLED",
                limit=(
                    "not run: rules are not formalized yet; "
                    "enable with agents.defaults.stepwiseZ3Check"
                ),
            )
        )

    def _check_rule_entailment(self, case: ReasoningCase) -> None:
        """Record solver status without treating SAT as proof of the claim."""

        premises = {
            fact.id: fact.status == "confirmed" for fact in case.facts if fact.status != "unknown"
        }
        claim_name = case.conclusion.statement if case.conclusion else ""
        if not premises or not claim_name:
            result = "SOLVER_UNKNOWN"
        else:
            result = check_entailment(premises, (claim_name, True)).value
        case.checks.append(
            CheckRecord(
                id=f"check-{len(case.checks) + 1}",
                target_version=f"{case.procedure_id}:{case.procedure_version}",
                kind="z3:entailment",
                result=result,
                limit="Boolean assignment consistency only; not natural-language truth",
                defects=[] if result != "INCONSISTENT" else ["premises conflict"],
            )
        )

    def _payload(
        self,
        case: ReasoningCase,
        stage: str,
        repair_defects: list[str] | None = None,
    ) -> dict[str, Any]:
        step = self._step(self._require_procedure(), stage)
        payload = self._case_fields(case, stage)
        # The step contract: what this call must do and which keys it must return.
        payload["question"] = step.question
        payload["expected_outputs"] = list(step.outputs)
        if step.role == "check" and "defects" not in payload["expected_outputs"]:
            # The check step reports findings; an absent list is not a pass.
            payload["expected_outputs"].append("defects")
        if step.role in _OUTPUT_SHAPES:
            # Typed roles are parsed into case objects; the model needs their shape.
            payload["output_shape"] = _OUTPUT_SHAPES[step.role]
        if repair_defects:
            payload["repair_defects"] = list(repair_defects)
        return payload

    def _case_fields(self, case: ReasoningCase, stage: str) -> dict[str, Any]:
        """Send the request, constraints, role fields and declared inputs only (design 12)."""

        procedure = self._require_procedure()
        step = self._step(procedure, stage)
        fields: dict[str, Any] = {"request": case.request, "stage": stage}
        if case.conversation and "request" in step.inputs:
            # Earlier turns resolve references in the request ("그럼 B는?").
            fields["conversation"] = case.conversation
        if case.goal:
            fields["goal"] = case.goal
        if case.constraints:
            fields["constraints"] = case.constraints
        values = {
            "facts": lambda: [item.model_dump() for item in case.facts],
            "criteria": lambda: [item.model_dump() for item in case.criteria],
            "judgments": lambda: [item.model_dump() for item in case.judgments],
            "conclusion": lambda: case.conclusion.model_dump() if case.conclusion else None,
        }
        for name in _ROLE_FIELDS.get(step.role, ()):
            value = values[name]()
            if value:
                fields[name] = value
        known = {item.id for item in procedure.steps}
        inputs: dict[str, Any] = {}
        for ref in step.inputs:
            if ref in known:
                output = {
                    key: value
                    for key, value in case.step_outputs.get(ref, {}).items()
                    if key not in fields
                }
                if output:
                    inputs[ref] = output
            elif ref != "request":
                matches = [item for item in procedure.requirements if item.split(":", 1)[0].strip() == ref]
                if matches:
                    inputs[ref] = matches[0]
        if inputs:
            fields["inputs"] = inputs
        return fields


# Case fields each typed role reads. Work steps read only their declared inputs.
_ROLE_FIELDS: dict[str, tuple[str, ...]] = {
    "goal": (),
    "facts": (),
    "criteria": ("facts",),
    "judgments": ("facts", "criteria"),
    "conclusion": ("criteria", "judgments"),
    "check": ("facts", "criteria", "judgments", "conclusion"),
    "answer": ("judgments", "conclusion"),
}


_OUTPUT_SHAPES: dict[str, dict[str, Any]] = {
    "goal": {
        "goal": "달성 목표 한 문장",
        "constraints": ["필수 제약"],
        "questions": ["핵심 정보가 없어 진행할 수 없을 때만 묻는 질문"],
        "assumptions": ["작은 가정"],
    },
    "facts": {
        "facts": [
            {
                "id": "f1",
                "proposition": "사실 한 문장",
                "source": "request 또는 출처",
                "status": "confirmed | estimated | unknown",
            }
        ]
    },
    "criteria": {
        "criteria": [
            {
                "id": "c1",
                "statement": "판정 가능한 기준",
                "kind": "required | preferred",
                "method": "판정 방법",
                "linked_goal": "입력의 goal을 그대로",
            }
        ]
    },
    "judgments": {
        "judgments": [
            {
                "id": "j1",
                "criterion_id": "c1",
                "result": "met | unmet | unknown",
                "evidence_ids": ["f1"],
                "assumptions": [],
            }
        ]
    },
    "conclusion": {
        "conclusion": {
            "id": "k1",
            "status": "suitable | conditionally_suitable | unsuitable | deferred",
            "statement": "결론 한 문장",
            "conditions": ["적용 조건"],
            "judgment_ids": ["j1"],
        }
    },
    "check": {
        "defects": [
            {
                "step": "결함을 고칠 앞 단계 id(사실 S2, 기준 S3, 판단 S4, 결론 S5 등)",
                "issue": "결론을 바꾸거나 규칙을 어기는 결함만 한 문장으로, 없으면 빈 배열",
            }
        ]
    },
    "answer": {"answer": "사용자에게 보일 최종 답 2~4문장"},
}


def _is_format_defect(defect: str) -> bool:
    return (
        "invalid shape" in defect
        or defect.startswith("declared output missing")
        or defect == "semantic check result missing"
    )


def _result_revision(payload: dict[str, Any], default: int) -> int | None:
    """Revision tag set by the adapter; malformed tags count as stale, not as a crash."""
    raw = payload.get("revision")
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _is_answer_step(step) -> bool:
    return step.role == "answer" or "answer" in step.outputs

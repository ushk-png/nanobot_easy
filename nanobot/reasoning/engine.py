"""Execute the method-evaluation procedure under code control."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from nanobot.reasoning.checks import (
    ANSWER_MISSING,
    answer_claim_check,
    check_stage,
    code_chain_defects,
    conclusion_rule_defects,
    selection_claim_defects,
    selection_template_answer,
)
from nanobot.reasoning.llm import BudgetExceededError, StageLLM
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
from nanobot.reasoning.selection import CODE_OPS
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
        max_check_returns: int = 1,
        on_stage: Callable[[str, str, str], None] | None = None,
        z3_check: bool = False,
    ) -> None:
        self.llm = llm
        self.reasoning_effort = reasoning_effort
        self.reasoning_disabled_guaranteed = reasoning_disabled_guaranteed
        self.max_revisions = max_revisions
        self.max_stage_calls = max_stage_calls
        # Returns a check step may trigger before its remaining findings become open issues.
        self.max_check_returns = max_check_returns
        self.on_stage = on_stage
        # Off until rules are actually formalized; a disabled check is recorded, never passed.
        self.z3_check = z3_check
        # First-step results produced by the routing call (design 10), consumed once.
        self._prefill: dict[str, dict[str, Any]] = {}
        self._procedure: Procedure | None = None

    def run(
        self,
        request: str,
        *,
        procedure_id: str = "method-evaluation",
        procedure: Procedure | None = None,
        case_id: str | None = None,
        conversation: list[dict[str, str]] | None = None,
        prefill: dict[str, dict[str, Any]] | None = None,
    ) -> ReasoningCase:
        procedure = procedure or load_procedure(procedure_id)
        self._procedure = procedure
        self._prefill = dict(prefill or {})
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
        repair: dict[str, dict[str, Any]] = {}
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
            step = self._step(procedure, stage)
            case.current_step = stage
            case.step_states[stage] = "running"
            step_calls[stage] = step_calls.get(stage, 0) + 1
            fix = repair.pop(stage, None)
            op_defects: list[str] = []
            called_model = False
            review_skipped: dict[str, Any] | None = None
            try:
                if step.op == "review":
                    decision = self._review_decision(case, procedure)
                    case.history.append({"event": "review_decision", "stage": stage, **decision})
                    if decision["run"]:
                        payload = self.llm.complete(stage, self._review_payload(case, stage, decision, fix))
                        called_model = True
                    else:
                        payload, review_skipped = {}, decision
                elif step.op != "llm":
                    # Code operations compute; no model call (design 8).
                    payload, op_defects = self._run_code_op(case, procedure, stage)
                elif stage in self._prefill and fix is None:
                    # Produced by the routing call; checked below like any step result.
                    payload = self._prefill.pop(stage)
                    case.history.append({"event": "prefilled", "stage": stage})
                else:
                    payload = self.llm.complete(stage, self._payload(case, stage, fix))
                    called_model = True
            except BudgetExceededError as exc:
                self._incomplete(case, stage, exc)
                break
            if called_model and fix is not None:
                # Beyond the default path: record why the extra call was made.
                case.history.append({"event": "extra_call", "stage": stage, "reason": fix["reason"]})
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
            if called_model:
                self._record_usage(case, payload)
            shape_defects = op_defects + self._apply(case, stage, payload, fix.get("only") if fix else None)
            if "check" in step.roles and case.procedure_id == "rule-judgment":
                if self.z3_check:
                    self._check_rule_entailment(case)
                else:
                    self._record_z3_disabled(case)
            if review_skipped is not None:
                defects = code_chain_defects(case)
                self._record_review_skipped(case, review_skipped)
            else:
                defects = shape_defects + [
                    defect
                    for key in self._check_keys(procedure, stage)
                    for defect in check_stage(
                        case,
                        key,
                        declared_outputs=step.outputs,
                        result=payload,
                        structured=procedure.granularity == "two_call",
                    )
                ]
                defects += self._structured_answer_defects(case, procedure, stage, payload)
            if defects and self._accept_open_issues(case, stage, defects):
                # A check that already sent the run back once: remaining model findings
                # are not passed as clean; see _accept_open_issues (design 6.2, 9).
                case.step_states[stage] = "conditionally_passed"
                self._notify_stage(stage, "passed")
                index += 1
                continue
            if defects:
                case.step_states[stage] = "needs_revision"
                if self._retry_same_step(step, defects, step_calls.get(stage, 0), case):
                    # Re-ask only this step, and only the outputs that are wrong (design 4.5).
                    case.revision_count += 1
                    repair[stage] = {
                        "defects": defects,
                        "only": self._partial_roles(procedure, step, defects),
                        "reason": "defects in this step's own output",
                    }
                    case.history.append({"event": "retry", "stage": stage, "defects": defects})
                    continue
                template = (
                    selection_template_answer(case)
                    if _is_answer_step(step) and procedure.answer_claims == "selection"
                    else None
                )
                if template:
                    # The explanation could not be made consistent: code writes it from
                    # the computed selection instead of stopping (approval item 4).
                    case.answer = template
                    case.history.append({"event": "template_answer", "stage": stage, "defects": defects})
                    case.step_states[stage] = "passed"
                    self._notify_stage(stage, "passed")
                    index += 1
                    continue
                if ANSWER_MISSING in defects and _is_answer_step(step):
                    self._stop(case, stage, ANSWER_MISSING)
                    case.status = "failed_empty_answer"
                    break
                target = self._defect_target(procedure, order, stage, payload)
                if not self._return_to(case, stage, target):
                    self._stop(case, stage, "; ".join(defects))
                    break
                # The step that fixes the defects gets them (design 9).
                repair[case.current_step] = {
                    "defects": defects,
                    "only": None,
                    "reason": f"returned from {stage}",
                }
                index = order.index(case.current_step)
                continue
            case.step_states[stage] = "passed"
            self._notify_stage(stage, "passed")
            if "goal" in step.roles and case.questions:
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

    def _partial_roles(self, procedure: Procedure, step, defects: list[str]) -> list[str] | None:
        """Two-call answer step: re-request only conclusion and answer if judgments are sound."""

        if procedure.granularity != "two_call" or not {"judgments", "conclusion", "answer"} <= set(step.roles):
            return None
        if any(_is_judgment_defect(item) for item in defects):
            return None
        return ["conclusion", "answer"]

    def _structured_answer_defects(
        self,
        case: ReasoningCase,
        procedure: Procedure,
        stage: str,
        payload: dict[str, Any],
    ) -> list[str]:
        """Two-call answer step: code-derived conclusion and structured answer agreement."""

        step = self._step(procedure, stage)
        if not _is_answer_step(step):
            return []
        if procedure.answer_claims == "selection":
            claims = case.step_outputs.get(stage, {}).get("answer_claims")
            return selection_claim_defects(case, claims)
        if procedure.answer_claims != "verdict":
            return []
        defects: list[str] = []
        if procedure.conclusion_rule == "required_criteria" and "conclusion" in step.roles:
            defects += conclusion_rule_defects(case)
        claims = case.step_outputs.get(stage, {}).get("answer_claims")
        claim_defects, additions = answer_claim_check(case, claims)
        defects += claim_defects
        if not defects and additions and case.answer.strip():
            # Template-fixable gaps are fixed in code, without another model call.
            case.answer = f"{case.answer.rstrip()} {' '.join(additions)}"
            case.history.append({"event": "template_fix", "stage": stage, "added": additions})
        return defects

    def _review_decision(self, case: ReasoningCase, procedure: Procedure) -> dict[str, Any]:
        """Conditional S6 (approval item 3): concrete risks only, never the verdict itself.

        Triggers: code-found evidence/judgment conflicts, model-flagged interpretation
        risks on required criteria, and conclusion rules code cannot check. An
        unsuitable/conditional/unknown verdict or a fact update alone does not trigger.
        """

        criteria = {item.id: item for item in case.criteria}
        facts = {item.id: item for item in case.facts}
        judgments = {item.id: item for item in case.judgments}
        items: list[dict[str, Any]] = []
        reasons: list[str] = []
        results_by_criterion: dict[str, set[str]] = {}
        for judgment in case.judgments:
            results_by_criterion.setdefault(judgment.criterion_id, set()).add(judgment.result)
            if judgment.result not in {"met", "unmet"}:
                continue
            cited = [facts[item] for item in judgment.evidence_ids if item in facts]
            if cited and all(item.status == "estimated" for item in cited):
                items.append(
                    {
                        "judgment_id": judgment.id,
                        "issue": "추정 사실만으로 충족·불충족을 판정했다",
                        "question": "확인된 근거로도 이 판정이 유지되는가?",
                    }
                )
                reasons.append("evidence_conflict")
            if judgment.assumptions:
                items.append(
                    {
                        "judgment_id": judgment.id,
                        "issue": f"가정에 기대어 판정했다: {'; '.join(judgment.assumptions)}",
                        "question": "가정 없이도 이 판정이 유지되는가?",
                    }
                )
                reasons.append("evidence_conflict")
        for criterion_id, results in results_by_criterion.items():
            if len(results) > 1:
                items.append(
                    {
                        "criterion_id": criterion_id,
                        "issue": "같은 기준에 서로 다른 판정이 있다",
                        "question": "어느 판정이 근거에 맞는가?",
                    }
                )
                reasons.append("evidence_conflict")
        ignored = 0
        for output in case.step_outputs.values():
            for risk in output.get("risks") or []:
                if not isinstance(risk, dict):
                    continue
                judgment = judgments.get(str(risk.get("judgment_id") or ""))
                criterion = criteria.get(judgment.criterion_id) if judgment else None
                if criterion is None or criterion.kind != "required":
                    ignored += 1  # cannot change the conclusion
                    continue
                items.append(
                    {
                        "judgment_id": judgment.id,
                        "issue": str(risk.get("issue") or "해석에 따라 판정이 달라질 수 있다"),
                        "question": str(risk.get("question") or "원문의 예외·부정·적용 범위를 어떻게 읽어야 하는가?"),
                    }
                )
                reasons.append("interpretation_risk")

        if case.conclusion is not None and case.conclusion.exception_rule.strip():
            items.append(
                {
                    "claim": "conclusion",
                    "issue": f"코드 결론 규칙과 다른 규칙을 적용했다: {case.conclusion.exception_rule}",
                    "question": "이 예외 규칙이 요청이나 규정에 근거하는가?",
                }
            )
            reasons.append("new_rule")
        policy = procedure.review_policy
        if policy == "required":
            reasons.insert(0, "policy_required")
            if not items:
                items = [
                    {"judgment_id": item.id, "issue": "필수 검토 절차", "question": "근거가 판정을 뒷받침하는가?"}
                    for item in case.judgments
                    if item.criterion_id in criteria and criteria[item.criterion_id].kind == "required"
                ]
        return {
            "run": policy == "required" or bool(items),
            "policy": policy,
            "reasons": sorted(set(reasons), key=reasons.index),
            "items": items,
            "ignored_risks": ignored,
        }

    def _review_payload(
        self,
        case: ReasoningCase,
        stage: str,
        decision: dict[str, Any],
        fix: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Only the questioned claims, their evidence and the question (approval item 3)."""

        judgments = {item.id: item for item in case.judgments}
        criteria = {item.id: item for item in case.criteria}
        facts = {item.id: item for item in case.facts}
        items: list[dict[str, Any]] = []
        for item in decision["items"]:
            entry: dict[str, Any] = {"issue": item["issue"], "question": item.get("question", "")}
            judgment = judgments.get(str(item.get("judgment_id") or ""))
            if judgment is not None:
                entry["judgment"] = judgment.model_dump()
                if judgment.criterion_id in criteria:
                    entry["criterion"] = criteria[judgment.criterion_id].model_dump()
                entry["evidence"] = [
                    facts[ref].model_dump() for ref in judgment.evidence_ids if ref in facts
                ]
            if item.get("criterion_id") in criteria:
                entry["criterion"] = criteria[item["criterion_id"]].model_dump()
                entry["judgments"] = [
                    value.model_dump() for value in case.judgments if value.criterion_id == item["criterion_id"]
                ]
            if item.get("claim") == "conclusion" and case.conclusion is not None:
                entry["conclusion"] = case.conclusion.model_dump()
            items.append(entry)
        procedure = self._require_procedure()
        order = self._execution_order(procedure)
        payload: dict[str, Any] = {
            "stage": stage,
            "question": "review_items의 주장만 함께 준 근거와 대조해 검토한다. 문제가 없으면 defects는 빈 배열이다.",
            "review_items": items,
            "expected_outputs": ["defects"],
            "output_shape": {
                "defects": [
                    {
                        "step": "고칠 앞 단계 id(fixable_steps 중)",
                        "judgment_id": "문제가 있는 판정 id(있으면)",
                        "issue": "결론을 바꾸는 문제만 한 문장으로",
                    }
                ]
            },
            "fixable_steps": [
                {"id": item, "roles": procedure.step(item).roles} for item in order[: order.index(stage)]
            ],
        }
        if fix:
            payload["repair_defects"] = list(fix["defects"])
        return payload

    def _record_review_skipped(self, case: ReasoningCase, decision: dict[str, Any]) -> None:
        reason = f"not triggered under policy '{decision['policy']}'"
        if decision.get("ignored_risks"):
            reason += f"; {decision['ignored_risks']} flagged risk(s) on non-required criteria ignored"
        case.checks.append(
            CheckRecord(
                id=f"check-{len(case.checks) + 1}",
                target_version=f"{case.procedure_id}:{case.procedure_version}",
                kind="llm:S6",
                result="SKIPPED",
                limit=f"{reason}; code chain checks still ran",
            )
        )

    def _accept_open_issues(self, case: ReasoningCase, stage: str, defects: list[str]) -> bool:
        step = self._step(self._require_procedure(), stage)
        if "check" not in step.roles or not all(item.startswith("semantic: ") for item in defects):
            return False
        returns = sum(
            1 for item in case.history if item.get("event") == "return" and item.get("from") == stage
        )
        if returns < self.max_check_returns:
            return False
        case.open_issues = [item.removeprefix("semantic: ") for item in defects]
        case.history.append({"event": "open_issues", "stage": stage, "issues": case.open_issues})
        procedure = self._require_procedure()
        order = self._execution_order(procedure)
        answered_before = any(
            _is_answer_step(procedure.step(item)) for item in order[: order.index(stage)]
        )
        if answered_before and case.answer.strip():
            # The answer was written before this review, so it cannot carry the issues.
            # Review items are conclusion-affecting, so the verdict is not final.
            if case.conclusion is not None:
                case.conclusion.status = "deferred"
            case.answer = (
                "점검에서 해결되지 않은 문제가 있어 결론을 확정하지 않았습니다(미확정): "
                f"{'; '.join(case.open_issues)}\n\n점검 전 판단: {case.answer.strip()}"
            )
            case.history.append({"event": "verdict_unconfirmed", "stage": stage})
        return True

    def _retry_same_step(self, step, defects: list[str], calls: int, case: ReasoningCase) -> bool:
        if step.op not in {"llm", "review"}:
            return False  # deterministic: the same inputs give the same result
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
            # Unaffected judgments are reused; only the others are judged again.
            case.kept_judgments = [item.id for item in case.judgments]
            case.conclusion = None
            case.answer = ""
            self._invalidate_from(case, self._first_role("judgments"))
            case.current_step = self._first_role("judgments")
            case.status = "running"
            case.history.append({"event": "facts_updated", "fact_ids": sorted(changed)})
        return case

    def _apply(
        self,
        case: ReasoningCase,
        stage: str,
        payload: dict[str, Any],
        only: list[str] | None = None,
    ) -> list[str]:
        """Store a stage result. Malformed typed output is a step defect, not a crash.

        ``only`` limits a partial re-request to some roles; the others keep their values.
        """

        step = self._step(self._require_procedure(), stage)
        role = step.role
        defects: list[str] = []
        for item_role in step.roles:
            if only is not None and item_role not in only:
                continue
            try:
                self._apply_role(case, item_role, payload)
            except ValidationError:
                defects.append(f"{item_role} output has an invalid shape")
        outputs = {key: payload[key] for key in step.outputs if key in payload and key != "usage"}
        if only is not None:
            case.step_outputs[stage] = {**case.step_outputs.get(stage, {}), **outputs}
        else:
            case.step_outputs[stage] = outputs
        if _is_answer_step(step):
            answer = payload.get("answer")
            case.answer = answer if isinstance(answer, str) else ""
        case.history.append({"event": "stage_result", "stage": stage, "role": role})
        return defects

    def _run_code_op(
        self,
        case: ReasoningCase,
        procedure: Procedure,
        stage: str,
    ) -> tuple[dict[str, Any], list[str]]:
        step = self._step(procedure, stage)
        operation = CODE_OPS.get(step.op)
        if operation is None:
            return {}, [f"unknown code operation: {step.op}"]
        inputs: dict[str, Any] = {}
        for ref in step.inputs:
            inputs.update(case.step_outputs.get(ref, {}))
        result, defects = operation(inputs)
        case.checks.append(
            CheckRecord(
                id=f"check-{len(case.checks) + 1}",
                target_version=f"{case.procedure_id}:{case.procedure_version}",
                kind=step.op,
                result="fail" if defects else "pass",
                limit="deterministic over the extracted table; extraction itself is not verified",
                defects=defects,
            )
        )
        return result, defects

    @staticmethod
    def _apply_role(case: ReasoningCase, role: str, payload: dict[str, Any]) -> None:
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
            judged = [Judgment.model_validate(item) for item in _as_list(payload.get("judgments"))]
            if case.kept_judgments:
                # Fact update: keep judgments that did not depend on changed facts and take
                # new ones only for the criteria that lost theirs (approval item 5).
                kept = [item for item in case.judgments if item.id in case.kept_judgments]
                covered = {item.criterion_id for item in kept}
                judged = kept + [item for item in judged if item.criterion_id not in covered]
                case.kept_judgments = []
            case.judgments = judged
        elif role == "conclusion":
            raw = payload.get("conclusion")
            case.conclusion = Conclusion.model_validate(raw) if isinstance(raw, dict) else None

    def _defect_target(
        self,
        procedure: Procedure,
        order: list[str],
        stage: str,
        payload: dict[str, Any],
    ) -> str | None:
        """Check steps name where each defect is fixed; code keeps only earlier steps (design 5, 9)."""

        if "check" not in self._step(procedure, stage).roles:
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
        if any("conclusion" in step.roles for step in steps):
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

    def _check_keys(self, procedure: Procedure, stage: str) -> list[str]:
        """Checks for every role the step covers; '' is the declared-output check."""

        step = self._step(procedure, stage)
        keys = [_ROLE_CHECKS[role] for role in step.roles if role in _ROLE_CHECKS]
        if _is_answer_step(step) and _ROLE_CHECKS["answer"] not in keys:
            keys.append(_ROLE_CHECKS["answer"])
        return keys or [""]

    def _first_role(self, role: str) -> str:
        procedure = self._require_procedure()
        for step in procedure.steps:
            if role in step.roles:
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

    def _incomplete(self, case: ReasoningCase, stage: str, exc: BudgetExceededError) -> None:
        """Budget used up: keep confirmed results, never report the run as completed."""

        case.status = "incomplete"
        case.step_states[stage] = "stopped"
        case.stop_reason = f"budget:{exc.kind}"
        case.history.append(
            {"event": "budget_exceeded", "stage": stage, "kind": exc.kind, "detail": exc.detail}
        )
        self._notify_stage(stage, "stopped")

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
        fix: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        procedure = self._require_procedure()
        step = self._step(procedure, stage)
        payload = self._case_fields(case, stage)
        # The step contract: what this call must do and which keys it must return.
        payload["question"] = step.question
        payload["expected_outputs"] = list(step.outputs)
        if "check" in step.roles and "defects" not in payload["expected_outputs"]:
            # The check step reports findings; an absent list is not a pass.
            payload["expected_outputs"].append("defects")
            procedure = self._require_procedure()
            order = self._execution_order(procedure)
            payload["fixable_steps"] = [
                {"id": item, "roles": procedure.step(item).roles}
                for item in order[: order.index(stage)]
            ]
        only = fix.get("only") if fix else None
        roles = [role for role in step.roles if only is None or role in only]
        shape: dict[str, Any] = {}
        for role in roles:
            # Typed roles are parsed into case objects; the model needs their shape.
            shape.update(_OUTPUT_SHAPES.get(role, {}))
        two_call_answer = procedure.answer_claims == "verdict" and _is_answer_step(step)
        if procedure.answer_claims == "selection" and _is_answer_step(step):
            shape.update(_SELECTION_ANSWER_EXTRAS)
        if two_call_answer and "answer" in roles:
            shape.update(_TWO_CALL_ANSWER_EXTRAS)
            if "conclusion" in shape:
                shape["conclusion"] = {**shape["conclusion"], **_EXCEPTION_RULE_SHAPE}
        if two_call_answer and procedure.conclusion_rule == "required_criteria":
            payload["conclusion_rule"] = _CONCLUSION_RULE_TEXT
        if shape:
            payload["output_shape"] = shape
        if only is not None:
            # Partial re-request: earlier outputs of this step are fixed inputs now.
            payload["expected_outputs"] = [key for key in step.outputs if key in shape]
            payload["fixed"] = {"judgments": [item.model_dump() for item in case.judgments]}
        if _is_answer_step(step) and case.open_issues:
            payload["open_issues"] = list(case.open_issues)
        if fix:
            payload["repair_defects"] = list(fix["defects"])
        return payload

    def _case_fields(self, case: ReasoningCase, stage: str) -> dict[str, Any]:
        """Send the request, constraints, role fields and declared inputs only (design 12)."""

        procedure = self._require_procedure()
        step = self._step(procedure, stage)
        fields: dict[str, Any] = {"request": case.request, "stage": stage}
        if case.conversation and ("request" in step.inputs or {"goal", "facts"} & set(step.roles)):
            # Earlier turns resolve references ("그럼 B는?") and hold data a follow-up
            # corrects; fact gathering needs them as much as the request step does.
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
        produced = set(step.roles)
        reads = [name for role in step.roles for name in _ROLE_FIELDS.get(role, ())]
        for name in dict.fromkeys(reads):
            if name in produced:
                continue  # a merged step writes this field itself
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
        if "judgments" in step.roles and case.kept_judgments:
            kept = [item for item in case.judgments if item.id in case.kept_judgments]
            covered = {item.criterion_id for item in kept}
            fields["kept_judgments"] = [item.model_dump() for item in kept]
            fields["rejudge_criteria"] = [item.id for item in case.criteria if item.id not in covered]
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
                "step": "결함을 고칠 앞 단계 id(fixable_steps 중 그 역할을 맡은 단계)",
                "issue": "결론을 바꾸거나 규칙을 어기는 결함만 한 문장으로, 없으면 빈 배열",
            }
        ]
    },
    "answer": {"answer": "사용자에게 보일 최종 답 2~4문장. open_issues가 있으면 미해결로 함께 밝힘"},
    "extract": {
        "table": {
            "candidates": [
                {"id": "A", "values": {"속성 이름": "숫자·true/false·문자열, 확인되지 않은 값은 null"}}
            ],
            "required": [
                {"attr": "values의 속성 이름과 같게", "op": ">= | <= | > | < | == | !=", "value": "비교 값"}
            ],
            "objective": {"attr": "고를 때 비교할 속성", "direction": "max | min"},
            "notes": ["단위 환산 등. 같은 속성은 모든 후보에서 같은 이름·단위의 숫자로. 정정된 값이 있으면 정정 값"],
        }
    },
}


_TWO_CALL_ANSWER_EXTRAS: dict[str, Any] = {
    "answer_claims": {
        "status": "answer가 전달하는 결론 상태(conclusion.status와 같게)",
        "covers": ["answer가 이유를 설명한 기준 id(불충족 필수 기준은 모두)"],
        "unknown_disclosed": ["answer가 미확인이라고 밝힌 기준 id"],
        "conditions_included": ["answer에 담은 conclusion.conditions의 순번(0부터)"],
    },
    "risks": [
        {
            "judgment_id": "원문의 예외·부정·적용 범위 해석에 따라 결론이 바뀔 수 있는 판정 id",
            "issue": "어떤 해석 차이인지 한 문장",
            "question": "확인할 질문",
        }
    ],
}
_SELECTION_ANSWER_EXTRAS: dict[str, Any] = {
    "answer_claims": {
        "selected": "answer가 선정한 후보 id(입력 selection.selected와 같게, 없으면 null)",
        "excluded": ["answer가 선정하지 않은 이유를 밝힌 후보 id(나머지 모두)"],
    }
}
_EXCEPTION_RULE_SHAPE = {
    "exception_rule": "요청·규정이 conclusion_rule과 다른 결론을 명시적으로 허용할 때만 그 근거, 아니면 생략",
}
_CONCLUSION_RULE_TEXT = (
    "필수 기준 중 하나라도 unmet이면 unsuitable. unmet 없이 unknown(또는 판정 없음)이 남으면 "
    "conditionally_suitable 또는 deferred. 필수 기준이 모두 met이면 suitable. "
    "선호 기준은 결론 상태를 바꾸지 않는다."
)
_JUDGMENT_DEFECT_MARKERS = (
    "criteria without judgment",
    "has no evidence",
    "references unknown facts",
    "promotes unknown evidence",
    "judgments output has an invalid shape",
)


def first_step_contract(procedure: Procedure) -> dict[str, Any]:
    """What a routing call must return to stand in for the procedure's first step."""

    step = procedure.steps[0]
    shape: dict[str, Any] = {}
    for role in step.roles:
        shape.update(_OUTPUT_SHAPES.get(role, {}))
    return {
        "id": step.id,
        "question": step.question,
        "expected_outputs": list(step.outputs),
        "output_shape": shape,
    }


def _is_judgment_defect(defect: str) -> bool:
    return any(marker in defect for marker in _JUDGMENT_DEFECT_MARKERS)


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

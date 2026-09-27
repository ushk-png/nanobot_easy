"""Workflow result evaluation for stage 4."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanobot.workflow.executor import WorkflowExecutionResult
from nanobot.workflow.schema import WorkflowEnvelope


@dataclass(slots=True)
class WorkflowEvaluationReport:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    severity: str = "ok"

    def model_dump(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": dict(self.checks),
            "issues": list(self.issues),
            "severity": self.severity,
        }


class WorkflowEvaluator:
    """Conservative post-run guard for composed workflows.

    The evaluator does not invent content.  It verifies that the envelope is
    internally consistent and that completed workflows have a non-empty final
    payload; if not, it converts the result to NEEDS_ATTENTION so the caller can
    recover instead of delivering an empty or inconsistent answer.
    """

    def evaluate(self, result: WorkflowExecutionResult) -> tuple[WorkflowExecutionResult, WorkflowEvaluationReport]:
        envelope = result.envelope
        checks: dict[str, bool] = {}
        issues: list[str] = []

        checks["has_task_id"] = bool(envelope.task_id)
        if not checks["has_task_id"]:
            issues.append("missing task_id")

        checks["state_present"] = bool(envelope.state)
        if not checks["state_present"]:
            issues.append("missing state")

        if envelope.state == "COMPLETED":
            checks["completed_has_final"] = envelope.deliver == "final" and bool((envelope.reason or "").strip())
            if not checks["completed_has_final"]:
                issues.append("completed workflow has no final answer")
        else:
            checks["completed_has_final"] = True

        if envelope.state == "WAITING_USER":
            checks["waiting_has_question"] = envelope.deliver == "question" and bool((envelope.question or "").strip()) and bool(envelope.question_id)
            if not checks["waiting_has_question"]:
                issues.append("waiting workflow has no question/question_id")
        else:
            checks["waiting_has_question"] = True

        if envelope.state in {"FAILED", "NEEDS_ATTENTION"}:
            checks["attention_has_reason"] = bool((envelope.reason or envelope.next_hint or "").strip())
            if not checks["attention_has_reason"]:
                issues.append("non-success workflow has no reason")
        else:
            checks["attention_has_reason"] = True

        passed = not issues
        report = WorkflowEvaluationReport(
            passed=passed,
            checks=checks,
            issues=issues,
            severity="ok" if passed else "needs_attention",
        )
        result.data.setdefault("evaluation", report.model_dump())
        if passed:
            return result, report
        fixed = envelope.model_copy(update={
            "state": "NEEDS_ATTENTION",
            "deliver": "none",
            "reason": "; ".join(issues),
            "next_hint": "Workflow result failed post-run evaluation; retry or ask the user for missing context.",
        })
        return WorkflowExecutionResult(envelope=fixed, data=result.data, trace=result.trace), report

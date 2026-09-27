"""Stage-5 quality comparison for workflow on/off answer runs.

The original design document chapter 11 requires a same-condition comparison of
workflow disabled (baseline) and workflow enabled runs, including prior failure
cases, held-out questions, task success, instruction compliance, grounding
accuracy, unnecessary actions, time, and cost.  This module is a report harness;
it does not claim that mocked rows prove real-provider quality improvement.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ORIGINAL_CHAPTER_11_SCENARIOS = [
    "sufficient_simple_question",
    "answer_in_current_context",
    "external_info_missing",
    "user_private_info_missing",
    "insufficient_answer_evidence",
    "invalid_json_or_unregistered_function_or_missing_step",
    "repeated_same_search",
    "forced_shutdown_recovery",
    "external_success_before_local_record_failure",
    "changed_user_response_or_instruction",
    "repeated_restart_budget_not_reset",
    "review_limit_or_search_unavailable",
    "existing_skill_required",
    "cross_user_or_project_data_isolation",
]


@dataclass(frozen=True)
class WorkflowQualityMetrics:
    question_count: int
    regression_case_count: int
    heldout_case_count: int
    repeated_measurement_count: int
    workflow_answered_count: int
    baseline_answered_count: int
    workflow_task_success_count: int
    baseline_task_success_count: int
    workflow_instruction_compliance_count: int
    baseline_instruction_compliance_count: int
    workflow_grounding_accuracy_count: int
    baseline_grounding_accuracy_count: int
    workflow_unnecessary_action_count: int
    baseline_unnecessary_action_count: int
    workflow_needs_attention_count: int
    baseline_needs_attention_count: int
    workflow_waiting_user_count: int
    baseline_waiting_user_count: int
    workflow_tool_call_count: int
    baseline_tool_call_count: int
    workflow_llm_call_count: int
    baseline_llm_call_count: int
    workflow_average_steps: float
    baseline_average_steps: float
    workflow_average_latency_ms: float
    baseline_average_latency_ms: float
    workflow_total_cost: float
    baseline_total_cost: float

    def model_dump(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _load_yaml_or_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _coerce_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = data.get("questions") or data.get("results") or data.get("cases") or []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _status(row: dict[str, Any]) -> str:
    return _text(row.get("state") or row.get("status") or row.get("outcome")).upper()


def _answer(row: dict[str, Any]) -> str:
    return _text(row.get("answer") or row.get("final") or row.get("response") or row.get("reason"))


def _int(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def _float(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def _bool(row: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "pass", "passed", "success", "ok"}
    return False


def _list(row: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            return value
    return []


def _float_average(values: list[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def _result_count(rows: list[dict[str, Any]], side: str, *keys: str) -> int:
    count = 0
    for row in rows:
        result = row.get(side) if isinstance(row.get(side), dict) else {}
        assert isinstance(result, dict)
        count += int(_bool(result, *keys))
    return count


def _unnecessary_action_count(rows: list[dict[str, Any]], side: str) -> int:
    count = 0
    for row in rows:
        result = row.get(side) if isinstance(row.get(side), dict) else {}
        assert isinstance(result, dict)
        count += _int(result, "unnecessary_actions", "unnecessary_action_count")
        if _bool(result, "unnecessary_search", "unnecessary_question", "unnecessary_tool"):
            count += 1
    return count


def compute_quality_metrics(rows: list[dict[str, Any]]) -> WorkflowQualityMetrics:
    question_count = len(rows)
    workflow_steps: list[float] = []
    baseline_steps: list[float] = []
    workflow_latency: list[float] = []
    baseline_latency: list[float] = []

    workflow_answered = baseline_answered = 0
    workflow_needs = baseline_needs = 0
    workflow_waiting = baseline_waiting = 0
    workflow_tool_calls = baseline_tool_calls = 0
    workflow_llm_calls = baseline_llm_calls = 0
    workflow_cost = baseline_cost = 0.0
    repeated_measurements = 0

    for row in rows:
        repeated_measurements += max(0, _int(row, "runs", "repeat_count", "measurement_count") - 1)
        workflow = row.get("workflow") if isinstance(row.get("workflow"), dict) else {}
        baseline = row.get("baseline") if isinstance(row.get("baseline"), dict) else {}
        assert isinstance(workflow, dict)
        assert isinstance(baseline, dict)
        if _answer(workflow):
            workflow_answered += 1
        if _answer(baseline):
            baseline_answered += 1
        workflow_needs += int(_status(workflow) == "NEEDS_ATTENTION")
        baseline_needs += int(_status(baseline) == "NEEDS_ATTENTION")
        workflow_waiting += int(_status(workflow) == "WAITING_USER")
        baseline_waiting += int(_status(baseline) == "WAITING_USER")
        workflow_tool_calls += _int(workflow, "tool_calls", "tool_call_count")
        baseline_tool_calls += _int(baseline, "tool_calls", "tool_call_count")
        workflow_llm_calls += _int(workflow, "llm_calls", "llm_call_count")
        baseline_llm_calls += _int(baseline, "llm_calls", "llm_call_count")
        workflow_steps.append(float(_int(workflow, "steps", "step_count")))
        baseline_steps.append(float(_int(baseline, "steps", "step_count")))
        workflow_latency.append(_float(workflow, "latency_ms", "duration_ms", "elapsed_ms"))
        baseline_latency.append(_float(baseline, "latency_ms", "duration_ms", "elapsed_ms"))
        workflow_cost += _float(workflow, "cost", "cost_usd", "estimated_cost")
        baseline_cost += _float(baseline, "cost", "cost_usd", "estimated_cost")

    return WorkflowQualityMetrics(
        question_count=question_count,
        regression_case_count=sum(1 for row in rows if _bool(row, "regression", "known_failure", "previous_failure")),
        heldout_case_count=sum(1 for row in rows if _bool(row, "heldout", "held_out", "not_used_in_development")),
        repeated_measurement_count=repeated_measurements,
        workflow_answered_count=workflow_answered,
        baseline_answered_count=baseline_answered,
        workflow_task_success_count=_result_count(rows, "workflow", "task_success", "success"),
        baseline_task_success_count=_result_count(rows, "baseline", "task_success", "success"),
        workflow_instruction_compliance_count=_result_count(rows, "workflow", "instruction_compliance", "instructions_followed"),
        baseline_instruction_compliance_count=_result_count(rows, "baseline", "instruction_compliance", "instructions_followed"),
        workflow_grounding_accuracy_count=_result_count(rows, "workflow", "grounding_accuracy", "grounded", "evidence_correct"),
        baseline_grounding_accuracy_count=_result_count(rows, "baseline", "grounding_accuracy", "grounded", "evidence_correct"),
        workflow_unnecessary_action_count=_unnecessary_action_count(rows, "workflow"),
        baseline_unnecessary_action_count=_unnecessary_action_count(rows, "baseline"),
        workflow_needs_attention_count=workflow_needs,
        baseline_needs_attention_count=baseline_needs,
        workflow_waiting_user_count=workflow_waiting,
        baseline_waiting_user_count=baseline_waiting,
        workflow_tool_call_count=workflow_tool_calls,
        baseline_tool_call_count=baseline_tool_calls,
        workflow_llm_call_count=workflow_llm_calls,
        baseline_llm_call_count=baseline_llm_calls,
        workflow_average_steps=_float_average(workflow_steps),
        baseline_average_steps=_float_average(baseline_steps),
        workflow_average_latency_ms=_float_average(workflow_latency),
        baseline_average_latency_ms=_float_average(baseline_latency),
        workflow_total_cost=workflow_cost,
        baseline_total_cost=baseline_cost,
    )


def validate_chapter11_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scenarios = {str(row.get("scenario") or row.get("scenario_id") or "") for row in rows}
    scenarios.discard("")
    missing = [scenario for scenario in ORIGINAL_CHAPTER_11_SCENARIOS if scenario not in scenarios]
    same_conditions = all(
        _bool(row, "same_model", "same_provider")
        and _bool(row, "same_materials", "same_sources")
        and _bool(row, "same_tools")
        and _bool(row, "same_skills")
        for row in rows
    ) if rows else False
    has_regression = any(_bool(row, "regression", "known_failure", "previous_failure") for row in rows)
    has_heldout = any(_bool(row, "heldout", "held_out", "not_used_in_development") for row in rows)
    has_repeated = any(_int(row, "runs", "repeat_count", "measurement_count") > 1 for row in rows)
    review_pass_accuracy_warning = any(
        _bool(result, "review_passed") and not _bool(result, "grounding_accuracy", "grounded", "evidence_correct")
        for row in rows
        for result in [row.get("workflow"), row.get("baseline")]
        if isinstance(result, dict)
    )
    return {
        "scenario_count": len(scenarios),
        "required_scenarios": list(ORIGINAL_CHAPTER_11_SCENARIOS),
        "missing_scenarios": missing,
        "same_conditions": same_conditions,
        "has_regression_cases": has_regression,
        "has_heldout_cases": has_heldout,
        "has_repeated_measurements": has_repeated,
        "review_pass_rate_treated_as_accuracy": False,
        "review_pass_accuracy_warning": review_pass_accuracy_warning,
    }


def build_quality_report(rows: list[dict[str, Any]], *, metric_source: str = "original-design-chapter-11") -> dict[str, Any]:
    metrics = compute_quality_metrics(rows)
    coverage = validate_chapter11_coverage(rows)
    return {
        "mode": "workflow_on_off_comparison",
        "metric_source": metric_source,
        "design_basis": {
            "document": "스킬 병행형 LLM 판단 워크플로우 설계서",
            "chapter": "11. 필수 인수 시나리오",
            "quality_dimensions": [
                "task_success_rate",
                "instruction_compliance",
                "grounding_accuracy",
                "unnecessary_actions",
                "time",
                "cost",
            ],
        },
        "note": (
            "Same-model, same-material, same-tool, same-skill workflow on/off comparison. "
            "Mocked rows verify the harness only; real-provider quality improvement must be "
            "reported separately from simulated acceptance coverage. Review pass rate is not "
            "treated as answer accuracy."
        ),
        "question_count": len(rows),
        "coverage": coverage,
        "metrics": metrics.model_dump(),
        "results": rows,
    }


def run_quality_report(config: Path | None = None, *, output: Path | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    if config is not None:
        loaded = _load_yaml_or_json(config)
        if isinstance(loaded, dict):
            cfg = loaded
            rows_path = cfg.get("results") or cfg.get("questions") or cfg.get("cases")
            if isinstance(rows_path, str):
                rows = _coerce_rows(_load_yaml_or_json(Path(rows_path)))
            else:
                rows = _coerce_rows(loaded)
        else:
            rows = _coerce_rows(loaded)
    metric_source = str(cfg.get("metric_source") or "original-design-chapter-11")
    report = build_quality_report(rows, metric_source=metric_source)
    report_path = output or (Path(str(cfg.get("report_path"))) if cfg.get("report_path") else None)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report

from __future__ import annotations

import json
from pathlib import Path

from nanobot.workflow.quality import (
    ORIGINAL_CHAPTER_11_SCENARIOS,
    build_quality_report,
    compute_quality_metrics,
    run_quality_report,
    validate_chapter11_coverage,
)


def _case(scenario: str, *, regression: bool = False, heldout: bool = False, runs: int = 1) -> dict:
    return {
        "id": scenario,
        "scenario": scenario,
        "question": f"question for {scenario}",
        "same_model": True,
        "same_materials": True,
        "same_tools": True,
        "same_skills": True,
        "regression": regression,
        "heldout": heldout,
        "runs": runs,
        "workflow": {
            "state": "COMPLETED",
            "answer": "answer",
            "task_success": True,
            "instruction_compliance": True,
            "grounding_accuracy": True,
            "tool_calls": 1,
            "llm_calls": 3,
            "steps": 4,
            "latency_ms": 1200,
            "cost_usd": 0.02,
        },
        "baseline": {
            "state": "COMPLETED",
            "answer": "other",
            "task_success": False,
            "instruction_compliance": True,
            "grounding_accuracy": False,
            "unnecessary_actions": 1,
            "tool_calls": 0,
            "llm_calls": 1,
            "steps": 1,
            "latency_ms": 400,
            "cost_usd": 0.01,
        },
    }


def test_quality_metrics_compare_original_chapter11_dimensions() -> None:
    rows = [
        _case("sufficient_simple_question", regression=True, runs=3),
        _case("answer_in_current_context", heldout=True),
        {
            **_case("user_private_info_missing"),
            "workflow": {"state": "WAITING_USER", "question": "선호?", "llm_calls": 2, "steps": 3, "latency_ms": 500},
            "baseline": {"state": "NEEDS_ATTENTION", "reason": "unclear", "llm_calls": 1, "steps": 1, "latency_ms": 200},
        },
    ]

    metrics = compute_quality_metrics(rows)

    assert metrics.question_count == 3
    assert metrics.regression_case_count == 1
    assert metrics.heldout_case_count == 1
    assert metrics.repeated_measurement_count == 2
    assert metrics.workflow_task_success_count == 2
    assert metrics.baseline_task_success_count == 0
    assert metrics.workflow_instruction_compliance_count == 2
    assert metrics.baseline_instruction_compliance_count == 2
    assert metrics.workflow_grounding_accuracy_count == 2
    assert metrics.baseline_grounding_accuracy_count == 0
    assert metrics.baseline_unnecessary_action_count == 2
    assert metrics.workflow_waiting_user_count == 1
    assert metrics.baseline_needs_attention_count == 1
    assert metrics.workflow_tool_call_count == 2
    assert metrics.workflow_average_steps == (4 + 4 + 3) / 3
    assert metrics.workflow_total_cost == 0.04


def test_chapter11_coverage_reports_required_scenarios_and_conditions() -> None:
    rows = [
        _case(scenario, regression=(idx == 0), heldout=(idx == 1), runs=(2 if idx == 2 else 1))
        for idx, scenario in enumerate(ORIGINAL_CHAPTER_11_SCENARIOS)
    ]

    coverage = validate_chapter11_coverage(rows)

    assert coverage["missing_scenarios"] == []
    assert coverage["same_conditions"] is True
    assert coverage["has_regression_cases"] is True
    assert coverage["has_heldout_cases"] is True
    assert coverage["has_repeated_measurements"] is True
    assert coverage["review_pass_rate_treated_as_accuracy"] is False


def test_quality_report_uses_original_chapter11_and_warns_against_mocked_quality_claims() -> None:
    report = build_quality_report([], metric_source="original-design-chapter-11")
    assert report["mode"] == "workflow_on_off_comparison"
    assert report["metric_source"] == "original-design-chapter-11"
    assert report["design_basis"]["chapter"] == "11. 필수 인수 시나리오"
    assert "task_success_rate" in report["design_basis"]["quality_dimensions"]
    assert "Review pass rate is not treated as answer accuracy" in report["note"]


def test_quality_report_flags_review_pass_not_accuracy() -> None:
    row = _case("insufficient_answer_evidence")
    row["workflow"]["review_passed"] = True
    row["workflow"]["grounding_accuracy"] = False
    report = build_quality_report([row])
    assert report["coverage"]["review_pass_accuracy_warning"] is True
    assert report["coverage"]["review_pass_rate_treated_as_accuracy"] is False


def test_quality_report_runner_writes_original_chapter11_report_from_yaml(tmp_path: Path) -> None:
    config = tmp_path / "workflow_quality.yaml"
    report_path = tmp_path / "report.json"
    config.write_text(
        "\n".join(
            [
                "metric_source: original-design-chapter-11",
                f"report_path: {report_path}",
                "questions:",
                "  - id: Q1",
                "    scenario: sufficient_simple_question",
                "    question: same question",
                "    same_model: true",
                "    same_materials: true",
                "    same_tools: true",
                "    same_skills: true",
                "    regression: true",
                "    heldout: true",
                "    runs: 2",
                "    workflow:",
                "      state: COMPLETED",
                "      answer: answer",
                "      task_success: true",
                "      instruction_compliance: true",
                "      grounding_accuracy: true",
                "      tool_calls: 1",
                "      llm_calls: 3",
                "      steps: 4",
                "      latency_ms: 1200",
                "      cost_usd: 0.02",
                "    baseline:",
                "      state: COMPLETED",
                "      answer: other",
                "      task_success: false",
                "      instruction_compliance: true",
                "      grounding_accuracy: false",
                "      unnecessary_actions: 1",
                "      tool_calls: 0",
                "      llm_calls: 1",
                "      steps: 1",
                "      latency_ms: 400",
                "      cost_usd: 0.01",
            ]
        ),
        encoding="utf-8",
    )

    report = run_quality_report(config)

    assert report_path.exists()
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["metric_source"] == "original-design-chapter-11"
    assert saved["metrics"]["workflow_task_success_count"] == 1
    assert report["metrics"]["baseline_unnecessary_action_count"] == 1
    assert report["coverage"]["has_regression_cases"] is True
    assert report["coverage"]["has_heldout_cases"] is True

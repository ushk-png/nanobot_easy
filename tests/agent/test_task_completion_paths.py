"""Keep normal and budget-exhausted completion behavior explicit during refactors."""

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ListDirTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import ToolCallRequest
from tests.agent.test_task_note import Script, control, final, run


@pytest.mark.parametrize("exhausted", [False, True], ids=["normal", "budget-exhausted"])
@pytest.mark.parametrize("outcome", [
    "completed", "cancelled", "partial", "blocked", "clarification",
    "missing_analysis", "missing_answer", "unconfirmed",
])
async def test_completion_preserves_report_status_and_call_budget(tmp_path, outcome, exhausted):
    report = "  확인 결과: 제공된 내용을 분석했습니다.\n"
    explanation = "외부 인증 확인이 필요합니다."
    changes = {"source": "none", "targets": [], "final_answer": report,
               "explanation": explanation}
    if outcome == "cancelled":
        changes.update(intent="withdraw", status="cancelled")
    elif outcome in {"partial", "blocked", "clarification"}:
        changes.update(status=outcome, analysis_complete=False)
    elif outcome == "missing_analysis":
        changes.update(analysis_complete=False)
    elif outcome == "missing_answer":
        changes.update(final_answer="")

    def response(kwargs):
        reply = (final("확인하지 않은 성공 보고") if outcome == "unconfirmed"
                 else control(answer="model draft", **changes)(kwargs))
        if exhausted:
            # A real non-observability tool keeps the loop running until its budget is spent.
            # Merely setting max_iterations=1 would still take the normal final path.
            reply.tool_calls.insert(0, ToolCallRequest(
                id="inspect", name="list_dir", arguments={"path": "."},
            ))
        return reply

    provider = Script([response, final("Budget finalization draft; not verified execution.")])
    tools = ToolRegistry()
    tools.register(ListDirTool(workspace=tmp_path))
    metadata = {}
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "제공된 내용을 분석하고 결과를 알려줘."}],
        tools=tools, model="test", max_iterations=1, max_tool_result_chars=8000,
        workspace=tmp_path, task_note_enabled=True, task_note_metadata=metadata,
    ))

    successful = outcome in {"completed", "cancelled"}
    expected_status = (outcome if successful
                       else "max_iterations" if exhausted else "incomplete")
    assert result.stop_reason == expected_status, result.final_content
    assert metadata["task_note"]["status"] == expected_status
    expected_calls = 2 if exhausted and not successful else 1
    assert len(provider.calls) == expected_calls
    assert metadata["task_note"]["metrics"]["model_calls"] == expected_calls
    assert (provider.calls[-1]["tools"] is None) == (expected_calls == 2)
    assert result.tools_used == (["list_dir"] if exhausted else []) + (
        ["update_task"] if outcome != "unconfirmed" else []
    )
    assert not list(tmp_path.iterdir())

    if successful:
        assert result.final_content == report.strip()
    elif outcome in {"partial", "blocked", "clarification"}:
        expected_report = report if exhausted else report.strip()
        assert result.final_content == expected_report + "\n\n미완료 상태: " + explanation
    else:
        defect = {"missing_analysis": "analysis_incomplete", "missing_answer": "answer_required",
                  "unconfirmed": "intent_unconfirmed"}[outcome]
        assert defect in result.final_content
        assert "model draft" not in result.final_content
        assert "Budget finalization draft" not in result.final_content


@pytest.mark.parametrize("max_iterations, expected_calls", [(1, 1), (3, 2)])
async def test_completion_recovers_missing_analysis_only_when_budget_remains(
    tmp_path, max_iterations, expected_calls,
):
    result, provider, _ = await run(tmp_path, [
        control(source="none", targets=[], analysis_complete=False, final_answer="분석 결과입니다."),
        control(source="none", targets=[], final_answer="분석 결과입니다."),
    ], request="제공된 내용을 분석해줘.", max_iterations=max_iterations)
    assert result.stop_reason == ("completed" if max_iterations > 1 else "incomplete")
    assert len(provider.calls) == expected_calls
    assert result.tools_used == ["update_task"] * expected_calls

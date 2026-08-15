import json

import pytest

from nanobot.agent.tools.registry import is_tool_error_result
from nanobot.agent.tools.student_learning import StudentLearningTool


@pytest.mark.asyncio
async def test_student_learning_upserts_review_queue(tmp_path):
    tool = StudentLearningTool(workspace=tmp_path)

    first = await tool.execute(
        action="upsert_review",
        subject="생명과학",
        concept="삼투압",
        due_date="2026-07-29",
    )
    second = await tool.execute(
        action="upsert_review",
        subject="생명과학",
        concept="삼투압",
        due_date="2026-08-02",
    )

    assert not is_tool_error_result("student_learning", first)
    payload = json.loads(second)
    assert payload["review"]["due_date"] == "2026-08-02"
    assert len(payload["review"]["review_history"]) == 2


@pytest.mark.asyncio
async def test_student_learning_logs_study(tmp_path):
    tool = StudentLearningTool(workspace=tmp_path)

    result = await tool.execute(
        action="log_study",
        subject="과학",
        concept="확산",
        next_action="복습",
    )

    assert not is_tool_error_result("student_learning", result)
    assert (tmp_path / "study_log.jsonl").exists()

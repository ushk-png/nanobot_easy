"""Public progress is sparse, specific, and never a substitute for completion checks."""
from unittest.mock import AsyncMock

from nanobot.agent.task_note import TaskNote, TaskUpdate
from nanobot.agent.task_progress import TaskProgress


def plan(note, **changes):
    return TaskUpdate.model_validate({
        "request_id": note.request_id, "intent": "new", "goal": "설계서와 실제 구현 대조",
        "targets": [], "source": "none", "analysis_complete": False, "status": "active",
        "explanation": "검토를 시작합니다.",
        "planned_steps": ["먼저 설계서와 실제 구현을 대조하겠습니다.", "이후 차이가 있는 부분을 수정하고 테스트하겠습니다."],
        **changes,
    })


def test_public_plan_is_specific_and_sent_once(tmp_path):
    now = [0.0]
    progress = TaskProgress(clock=lambda: now[0])
    note = TaskNote([{"role": "user", "content": "설계서 검토"}], tmp_path, {})
    update = plan(note)
    notice = progress.plan(update)
    assert "설계서와 실제 구현" in notice
    assert "이후" in notice and "테스트" in notice
    now[0] = 100
    assert progress.plan(update) is None


def test_read_bursts_are_coalesced_but_new_phase_explained():
    now = [0.0]
    progress = TaskProgress(clock=lambda: now[0])
    first = progress.operation("read_file", {"path": "/workspace/design.md"})
    assert "design.md" in first and "읽고" in first
    now[0] = 5
    assert progress.operation("read_file", {"path": "/workspace/runner.py"}) is None
    now[0] = 30
    assert progress.operation("read_file", {"path": "/workspace/runner.py"}) is None
    notice = progress.operation("exec", {"command": ".venv/bin/python -m pytest tests/test_runner.py -q"})
    assert "test_runner.py" in notice and "검증" in notice
    now[0] = 35
    assert progress.operation("edit_file", {"path": "runner.py"}) is None
    now[0] = 60
    assert "변경을 적용" in progress.operation("edit_file", {"path": "runner.py"})


def test_same_notice_never_repeats_and_read_range_is_observable():
    now = [0.0]
    progress = TaskProgress(clock=lambda: now[0])
    args = {"path": "design.md", "offset": 1, "limit": 100}
    assert "1~100행" in progress.operation("read_file", args)
    now[0] = 120
    assert progress.operation("read_file", args) is None
    assert "101~200행" in progress.operation("read_file", {**args, "offset": 101})


def test_internal_tools_and_completed_plan_do_not_emit(tmp_path):
    progress = TaskProgress()
    for name in ("skill_search", "skill_decision", "update_task", "complete_goal"):
        assert progress.operation(name, {}) is None
    note = TaskNote([{"role": "user", "content": "코드 검토"}], tmp_path, {})
    assert progress.plan(plan(note, status="completed", analysis_complete=True)) is None
    assert progress.plan(plan(note, planned_steps=["저장했습니다."])) is None


def test_followup_restarts_current_plan_without_reusing_old_plan(tmp_path):
    note = TaskNote([{"role": "user", "content": "보고서 수정"}], tmp_path, {})
    assert note.progress.plan(plan(note))
    note.revise([{"role": "user", "content": "저장하지 말고 검토만 해"}])
    assert note.update is None
    changed = plan(note, intent="change", planned_steps=["저장 없이 보고서의 문제점만 검토하겠습니다."])
    assert "저장 없이" in note.progress.plan(changed)


def test_verification_does_not_expose_shell_command_or_assert_success():
    progress = TaskProgress()
    notice = progress.operation("exec", {"command": ".venv/bin/ruff check nanobot/agent/runner.py"})
    assert "runner.py" in notice
    assert ".venv" not in notice and "ruff check" not in notice and "성공" not in notice


async def test_runner_sends_one_plan_instead_of_generic_notice_per_call(tmp_path):
    from nanobot.agent.runner import AgentRunner, AgentRunSpec
    from nanobot.agent.task_note import active_task_note
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.providers.base import LLMResponse, ToolCallRequest

    (tmp_path / "a.md").write_text("actual source", encoding="utf-8")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    provider = type("Provider", (), {})()
    count = 0

    async def chat_with_retry(**kwargs):
        nonlocal count
        count += 1
        note = active_task_note.get()
        if count in (1, 4):
            update = plan(note, targets=[{"path": "a.md", "selection_reason": "user target"}],
                          source="files", analysis_complete=count == 4,
                          status="completed" if count == 4 else "active",
                          final_answer="검토 결과입니다." if count == 4 else "")
            return LLMResponse(content="검토 결과입니다." if count == 4 else "",
                               tool_calls=[ToolCallRequest(id=str(count), name="update_task",
                                                           arguments=update.model_dump())])
        return LLMResponse(content="", tool_calls=[ToolCallRequest(
            id=str(count), name="read_file", arguments={"path": "a.md", "force": True})])

    provider.chat_with_retry = chat_with_retry
    notices = AsyncMock()
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 파일 검토"}], tools=tools,
        model="test", max_iterations=8, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}, progress_callback=notices))
    assert result.stop_reason == "completed"
    assert count == 4
    assert notices.await_count == 1
    message = notices.await_args.args[0]
    assert "먼저" in message and "이후" in message
    assert "요청한 자료를 확인하며 작업을 진행하고 있습니다" not in message

"""The original 11 requirements counterexamples, now mandatory regressions.

Every provider is mocked and all files/databases are temporary. No live LLMs.
The original invariant assertions are unchanged.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.agent.context_selection import (
    ContextSelector,
    RecordContextCompletionTool,
    prepare_selected_loop,
    validate_meaning,
)
from nanobot.agent.context_trace import active_context_trace
from nanobot.agent.hook import AgentHook
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.config.schema import ContextSelectionConfig
from nanobot.memory.task_context import TaskContextStore
from nanobot.providers.base import LLMResponse, ToolCallRequest

from .test_context_selection import FakeProvider, event, loop_spec, need, plan, select
from .test_context_selection import env as env


@pytest.mark.asyncio
async def test_required_memory_fact_cannot_be_satisfied_by_request_text_alone(env):
    current = event(env, "승인 문서를 확보한 뒤 설정 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"], information_needs=[need(
            question="승인 문서에 기록된 실제 승인값은 무엇인가?", query="",
            source_ids=[current.event_id], acceptance="원본 문서의 approval=APPROVED 확인")]),
        request=current.content)
    assert "n1" in selected.missing, "The request is not the requested approval document"


@pytest.mark.asyncio
async def test_reading_unrelated_file_cannot_unlock_required_file_change(env, tmp_path):
    (tmp_path / "unrelated.txt").write_text("This is not the required configuration")
    env[2].register(ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path))
    request = "settings.json 원문을 확인한 뒤 수정해줘"
    event(env, request)
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"], information_needs=[need(
            source_kind="file", query="settings.json", acceptance="settings.json 원문과 현재 버전 확인")]),
        request=request)
    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("wrong-read", "read_file", {"path": "unrelated.txt"})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("resolve", "resolve_context_need", {
            "need_id": "n1", "tool_call_id": "wrong-read", "finding": "Configuration verified"})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("change", "change_test_file", {})]),
        LLMResponse(content="Stopped")])
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": request}], tools=selected.tools,
        model="offline", max_iterations=4, max_tool_result_chars=10000, selected_context=selected))
    assert env[2].get("change_test_file").calls == 0, "An unrelated read unlocked mutation"


@pytest.mark.asyncio
async def test_successful_unrelated_read_cannot_prove_tests_passed(env):
    event(env, "테스트를 실행하고 결과를 검증해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="investigate", answer=None,
        completion_checks=["Integration tests executed and passed"]), request="테스트 결과를 검증해줘")
    selected.observations["readme"] = {"name": "read_file", "status": "ok", "read_only": True}
    result = await RecordContextCompletionTool(selected).execute(
        "Integration tests executed and passed", ["readme"], "passed", "All tests passed")
    assert getattr(result, "is_error", False), "A read success is not a test result"


@pytest.mark.asyncio
async def test_queued_cancellation_is_checked_before_tools_without_task_note(env):
    request = "파일 수정해줘"
    event(env, request)
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"]), request=request)
    pending = []

    class CancelBeforeTool(AgentHook):
        async def before_execute_tools(self, context):
            pending.append({"role": "user", "content": "취소. 파일을 수정하지 마."})

    async def drain():
        items = pending[:]
        pending.clear()
        return items

    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("change", "change_test_file", {})]),
        LLMResponse(content="취소 요청을 확인했습니다.")])
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": request}], tools=selected.tools,
        model="offline", max_iterations=3, max_tool_result_chars=1000, selected_context=selected,
        hook=CancelBeforeTool(), injection_callback=drain))
    assert env[2].get("change_test_file").calls == 0, "Cancellation was available before the write"


@pytest.mark.asyncio
async def test_user_cancellation_supersedes_old_goal_and_unfinished_item(env):
    event(env, "A를 설명해줘")
    selector, _, first = await select(env, plan(goal="Explain A", route="retrieve", answer=None,
        completion_checks=["Deliver A"]), request="A를 설명해줘")
    request = "A 설명은 취소하고 B만 설명해줘."
    event(env, request)
    design = plan(goal="Explain B", task_relation="change", task_id=first.state["task_id"],
                  completion_checks=["Deliver B"], answer="B는 두 번째 선택지입니다.")
    _, _, selected = await select(env, design, request=request)
    selector.finish(selected, [{"role": "assistant", "content": design.answer}],
                    stop_reason="completed", had_injections=False)
    task = TaskContextStore(env[0]).list(env[1], "s")[0]
    assert "Deliver A" not in task["unfinished"], "Cancelled work remains mandatory"
    assert task["goal"] == "Explain B"


@pytest.mark.asyncio
async def test_continue_does_not_import_all_unrelated_users_as_task_constraints(env):
    unrelated = event(env, "무관한 예전 대화 " * 300)
    event(env, "프로젝트 A를 설명해줘")
    _, _, first = await select(env, plan(route="retrieve", answer=None), request="프로젝트 A를 설명해줘")
    event(env, "계속해줘")
    _, _, continued = await select(env, plan(route="retrieve", answer=None,
        task_relation="continue", task_id=first.state["task_id"]), request="계속해줘")
    assert unrelated.event_id not in {c["event_id"] for c in continued.state["user_constraints"]}


def test_untrusted_document_summary_cannot_remove_required_original_fact(env):
    spec = loop_spec(env)
    critical = "CRITICAL_REQUIRED_FACT: the target database is read-only"
    messages = [{"role": "user", "content": "Read and preserve all database restrictions"}]
    for index in range(3):
        cid = f"read-{index}"
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": cid, "type": "function",
                "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": cid, "name": "read_file", "content": json.dumps({
                "context_summary": {"facts": ["A document"], "status": "ok",
                                    "error": None, "verification": "not checked"},
                "details": critical if index == 0 else "other data"})}])
    model = prepare_selected_loop(spec, messages, FakeProvider([]))
    assert critical in json.dumps(model), "An arbitrary document field discarded required data"


@pytest.mark.asyncio
async def test_iteration_finalizer_must_not_claim_completion_with_missing_evidence(env):
    event(env, "필수 파일을 확인하고 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        information_needs=[need(source_kind="file")]), request="필수 파일을 확인하고 수정해줘")
    assert selected.missing == ["n1"]
    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("read", "read_file", {})]),
        LLMResponse(content="설정을 변경하고 검증을 완료했습니다.")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "필수 파일을 확인하고 수정해줘"}],
        tools=selected.tools, model="offline", max_iterations=1, max_tool_result_chars=1000,
        selected_context=selected, finalize_on_max_iterations=True))
    assert "완료했습니다" not in result.final_content, "Finalizer skipped the missing-evidence gate"


@pytest.mark.asyncio
async def test_explicit_no_edit_constraint_rejects_edit_goal_in_semantic_validation(env):
    request = "파일은 읽기만 하세요. 절대로 수정하지 마세요."
    event(env, request)
    payload = ContextSelector(env[0], ContextSelectionConfig()).minimal_input(
        request, env[1], "s", env[2], env[3])
    errors = validate_meaning(plan(goal="파일을 수정한다", route="execute", request_type="modify",
        answer=None, capabilities_needed=["change_test_file"]), payload)
    assert errors, "Explicit no-edit instruction did not invalidate an edit goal"


@pytest.mark.asyncio
async def test_direct_followup_must_not_skip_unloaded_positive_task_constraints(env):
    original = event(env, "이 작업의 출력 언어는 한국어이며 예산은 0원입니다.")
    _, _, first = await select(env, plan(route="retrieve", answer=None), request="설명해줘")
    for index in range(45):
        event(env, f"무관한 질문 {index}")
        event(env, f"무관한 답변 {index}", actor="assistant")
    request = "같은 조건으로 간단한 예시를 들어줘"
    event(env, request)
    payload = ContextSelector(env[0], ContextSelectionConfig()).minimal_input(
        request, env[1], "s", env[2], env[3])
    assert payload["task_state_status"] == "stale"
    assert original.event_id not in {s["event_id"] for s in payload["sources"]}
    design = plan(task_relation="continue", task_id=first.state["task_id"], answer="Here is an example.")
    assert validate_meaning(design, payload), "Direct answer omitted unretrieved task constraints"


@pytest.mark.asyncio
async def test_cancelled_execution_persists_all_already_attempted_model_calls(env):
    event(env, "필수 파일을 조사해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="investigate", answer=None),
                                  request="필수 파일을 조사해줘")
    entered = asyncio.Event()

    class Waiting(FakeProvider):
        async def chat(self, messages, **kwargs):
            entered.set()
            await asyncio.sleep(10)

    async def run():
        selected.trace.stage = "execute"
        token = active_context_trace.set(selected.trace)
        try:
            await AgentRunner(Waiting([])).run(AgentRunSpec(
                initial_messages=[{"role": "user", "content": "필수 파일을 조사해줘"}],
                tools=selected.tools, model="offline", max_iterations=2,
                max_tool_result_chars=1000, selected_context=selected))
        finally:
            active_context_trace.reset(token)

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with env[0].connect() as conn:
        persisted = json.loads(conn.execute(
            "SELECT metrics FROM context_selection_runs WHERE request_id=?",
            (selected.trace.request_id,)).fetchone()[0])
    assert len(persisted["calls"]) == len(selected.trace.calls), "Execution cancellation lost call accounting"

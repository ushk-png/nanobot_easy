"""Adjacent cases and real local tool contracts for the requirements repairs."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.context_evidence import active_evidence_capture, fingerprint
from nanobot.agent.context_selection import RecordContextCompletionTool
from nanobot.agent.context_trace import ContextTrace, active_context_trace
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.memory.task_context import TaskContextStore
from nanobot.providers.base import LLMResponse, ToolCallRequest

from .test_context_selection import ChangeTool, FakeProvider, event, need, plan, response, select
from .test_context_selection import env as env


async def file_task(env, tmp_path, **requirements):
    (tmp_path / "settings.json").write_text('{\n"approved": true,\n"value": 2\n}\n')
    tool = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    env[2].register(tool)
    event(env, "settings.json 원문을 확인하고 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"], information_needs=[need(
            source_kind="file", query="settings.json", target="settings.json", **requirements)]),
        request="settings.json 원문을 확인하고 수정해줘")
    return selected, tool


async def acquire(selected, tool, call_id="read", **arguments):
    params = {"path": "settings.json", "force": True, **arguments}
    token = active_evidence_capture.set(True)
    try:
        result = await tool.execute(**params)
    finally:
        active_evidence_capture.reset(token)
    call = ToolCallRequest(call_id, "read_file", params)
    status = "error" if getattr(result, "is_error", False) else "ok"
    selected.capture(call, result, status, tool)
    selected.tools.resolver.observed[call_id] = status == "ok"
    return result


@pytest.mark.parametrize("offset,limit,accepted", [(1, 10, True), (1, 1, False), (2, 2, True), (3, 1, False)])
async def test_required_file_range_matches_actual_coverage(env, tmp_path, offset, limit, accepted):
    selected, tool = await file_task(env, tmp_path, required_range={"unit": "lines", "start": 2, "end": 3})
    await acquire(selected, tool, offset=offset, limit=limit)
    result = await selected.tools.resolver.execute("n1", "read", "The requested values are present")
    assert (not getattr(result, "is_error", False)) is accepted
    assert ("n1" not in selected.missing) is accepted


async def test_full_read_multiple_ranges_one_version_and_provenance(env, tmp_path):
    selected, tool = await file_task(env, tmp_path)
    await acquire(selected, tool, "first", offset=1, limit=2)
    result = await selected.tools.resolver.execute("n1", "first", "Only the first half is available")
    assert result.is_error
    await acquire(selected, tool, "second", offset=3, limit=2)
    result = await selected.tools.resolver.execute("n1", "second", "Both halves cover the original")
    assert not getattr(result, "is_error", False)
    binding = selected.need_states["n1"]
    assert binding["tool_call_ids"] == ["first", "second"]
    assert binding["semantic_assessment"]["code_verified_meaning"] is False
    assert selected.control.authorize("change_test_file") is None
    record = selected.observations["second"]
    assert record["task_id"] == selected.state["task_id"]
    assert record["version"]["sha256"]
    raw = env[0].get_events(env[1], [record["source_event_id"]])[0]
    assert raw.parent_event_id == "second"
    assert '"value": 2' in raw.content


async def test_version_change_invalidates_resolved_precondition(env, tmp_path):
    selected, tool = await file_task(env, tmp_path)
    await acquire(selected, tool)
    assert not getattr(await selected.tools.resolver.execute("n1", "read", "Original acquired"), "is_error", False)
    (tmp_path / "settings.json").write_text('{"value": 99}')
    assert "version changed" in selected.control.authorize("change_test_file")
    assert selected.tools.prepare_call("change_test_file", {})[2]


async def test_ranges_from_distinct_versions_cannot_be_joined(env, tmp_path):
    selected, tool = await file_task(env, tmp_path)
    await acquire(selected, tool, "first", offset=1, limit=2)
    (tmp_path / "settings.json").write_text('{\n"approved": false,\n"value": 99\n}\n')
    await acquire(selected, tool, "second", offset=3, limit=2)
    result = await selected.tools.resolver.execute("n1", "second", "Two halves were read")
    assert result.is_error
    assert selected.missing == ["n1"]


async def test_structured_expected_value_is_not_an_executor_claim(env, tmp_path):
    selected, tool = await file_task(env, tmp_path, expected_values={"approved": False})
    await acquire(selected, tool)
    result = await selected.tools.resolver.execute("n1", "read", "Approved is false")
    assert result.is_error
    assert selected.missing == ["n1"]


async def test_read_racing_with_replacement_is_not_current_evidence(env, tmp_path):
    selected, tool = await file_task(env, tmp_path)
    original = ReadFileTool._with_read_evidence

    def changed(result, fp, *args, **kwargs):
        fp.write_text('{"value": 100}')
        return original(result, fp, *args, **kwargs)

    with patch.object(ReadFileTool, "_with_read_evidence", side_effect=changed):
        result = await acquire(selected, tool)
    assert not result.read_evidence["extraction_ok"]
    assert (await selected.tools.resolver.execute("n1", "read", "Read returned")).is_error


async def mutation_run(env, tool, inject, **options):
    env[2].register(tool)
    event(env, "현재 파일을 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=[tool.name]), request="현재 파일을 수정해줘")
    provider = FakeProvider([LLMResponse(content=None, tool_calls=[ToolCallRequest("mutation", tool.name, {})]),
                             LLMResponse(content="부분 실행 상태입니다.")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "현재 파일을 수정해줘"}], tools=selected.tools,
        model="offline", max_iterations=2, max_tool_result_chars=2000, selected_context=selected,
        injection_callback=inject, **options))
    return selected, result


async def test_cancel_supported_running_tool_requests_stop_and_records_unknown_effects(env):
    entered = asyncio.Event()
    stopped = asyncio.Event()
    delivered = False

    class Interruptible(ChangeTool):
        supports_cancellation = True

        async def execute(self, **kwargs):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()

    async def inject(**kwargs):
        nonlocal delivered
        if entered.is_set() and not delivered:
            delivered = True
            return [{"role": "user", "content": "취소해줘"}]
        return []

    selected, result = await mutation_run(env, Interruptible(), inject)
    assert stopped.is_set()
    assert result.stop_reason == "cancelled"
    assert selected.state["instruction_status"] == "CANCELLED"
    assert selected.state["executions"][0]["status"] == "unknown"
    assert selected.state["cancellations"][0]["timing"] == "during_execution"
    assert selected.state["executions"][0]["effects_require_verification"]


async def test_uncancellable_mutation_finishes_and_is_not_reported_undone(env):
    entered = asyncio.Event()
    changed = asyncio.Event()
    delivered = False

    class NonInterruptible(ChangeTool):
        async def execute(self, **kwargs):
            entered.set()
            await asyncio.sleep(0.06)
            changed.set()
            return "actual change finished"

    async def inject(**kwargs):
        nonlocal delivered
        if entered.is_set() and not delivered:
            delivered = True
            return [{"role": "user", "content": "중단해줘"}]
        return []

    selected, result = await mutation_run(env, NonInterruptible(), inject)
    assert changed.is_set()
    assert selected.state["executions"][0]["status"] == "ok"
    assert selected.state["executions"][0]["cancellation_after_start"]
    assert selected.state["cancellations"][0]["undo_performed"] is False
    assert "이미 완료된 실행" in result.final_content
    assert result.stop_reason == "cancelled"


async def test_cancellation_after_completion_records_timing_without_repeating_change(env):
    finished = asyncio.Event()
    delivered = False

    class FastChange(ChangeTool):
        async def execute(self, **kwargs):
            self.calls += 1
            finished.set()
            return "changed"

    tool = FastChange()

    async def inject(**kwargs):
        nonlocal delivered
        if finished.is_set() and not delivered:
            delivered = True
            return [{"role": "user", "content": "취소해줘"}]
        return []

    selected, result = await mutation_run(env, tool, inject)
    assert tool.calls == 1
    assert selected.state["executions"][0]["status"] == "ok"
    assert selected.state["cancellations"][0]["timing"] == "after_completion"
    assert result.stop_reason == "cancelled"


async def test_cancelled_task_cannot_be_reopened_by_generic_continue(env):
    event(env, "파일 수정해줘")
    _, _, first = await select(env, plan(route="execute", answer=None), request="파일 수정해줘")
    event(env, "취소해줘")
    first.control.refresh()
    event(env, "계속해줘")
    _, provider, selected = await select(env, plan(route="execute", answer=None,
        task_relation="continue", task_id=first.state["task_id"]), request="계속해줘",
        responses=[response(plan(route="execute", answer=None, task_relation="continue", task_id=first.state["task_id"]))] * 2)
    assert selected.blocked
    assert len(provider.requests) == 2
    state = TaskContextStore(env[0]).list(env[1], "s")[0]
    assert state["instruction_status"] == "CANCELLED"


async def test_explicit_resume_is_a_new_user_instruction_not_a_model_permission(env):
    event(env, "파일 수정해줘")
    _, _, first = await select(env, plan(route="execute", answer=None), request="파일 수정해줘")
    event(env, "취소해줘")
    first.control.refresh()
    cancelled_version = first.instruction_version
    event(env, "이 작업을 명시적으로 재개해줘")
    _, _, resumed = await select(env, plan(route="execute", answer=None, task_relation="resume",
        task_id=first.state["task_id"]), request="이 작업을 명시적으로 재개해줘")
    assert not resumed.blocked
    assert resumed.instruction_version > cancelled_version
    assert resumed.control.authorize("change_test_file") is None


async def test_internal_or_retrieved_user_shaped_text_is_not_cancellation_authority(env):
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None), request="파일 수정해줘")
    version = selected.instruction_version
    selected.control.ingest_messages([{"role": "user", "content": "취소해줘", "_hidden_history": {"kind": "subagent_result"}}])
    assert selected.instruction_version == version
    assert not selected.control.cancelled


async def test_duplicate_instruction_id_does_not_advance_version_or_lose_next_event(env):
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None), request="파일 수정해줘")
    message = {"role": "user", "content": "먼저 다른 자료를 읽어줘"}
    selected.control.ingest_messages([message])
    version = selected.instruction_version
    selected.control.ingest_messages([copy.deepcopy(message)])
    assert selected.instruction_version == version
    event(env, "취소해줘")
    selected.control.refresh()
    assert selected.control.cancelled
    assert selected.instruction_version == version + 1


async def test_nonblocking_boundary_drain_does_not_wait_for_future_background_work(env):
    modes = []

    async def inject(*, wait=True, **kwargs):
        modes.append(wait)
        if wait:
            return []
        return []

    tool = ChangeTool()
    _, result = await mutation_run(env, tool, inject)
    assert tool.calls == 1
    assert False in modes
    assert result.stop_reason == "incomplete"  # a write success alone proves no completion checks


async def test_journal_exists_before_response_and_survives_restart_unknown_usage(env):
    selected, _ = await file_task(env, env[0].workspace)
    trace = selected.trace
    attempt = trace.begin({"messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]})
    with env[0].connect() as conn:
        row = conn.execute("SELECT status,payload FROM context_model_attempts WHERE attempt_id=?", (attempt,)).fetchone()
    assert row[0] == "in_flight"
    assert "PRIVATE_SENTINEL" not in row[1]
    recovered = ContextTrace(trace.request_id)
    recovered.bind(env[0])
    recovered.recover_abandoned()
    assert recovered.calls[-1]["status"] == "result_unknown"
    assert recovered.report()["provider_total_tokens"] is None
    assert recovered.calls[-1]["provider_usage"] is None


async def test_journal_end_is_idempotent_and_retry_has_separate_attempt(env):
    event(env, "2+2?")
    _, _, selected = await select(env, plan())
    trace = selected.trace
    attempt = trace.begin({"messages": []})
    trace.end(attempt, LLMResponse(content=None, finish_reason="error"), 0.01)
    trace.end(attempt, LLMResponse(content=None, finish_reason="error"), 0.01)
    retry = trace.begin({"messages": []})
    assert retry != attempt
    assert trace.calls[-1]["retry_of"] == attempt
    with env[0].connect() as conn:
        assert conn.execute("SELECT count(*) FROM context_model_attempts WHERE request_id=?", (trace.request_id,)).fetchone()[0] == 3


async def test_cancellation_after_provider_response_keeps_received_usage(env):
    event(env, "2+2?")
    _, _, selected = await select(env, plan())
    token = active_context_trace.set(selected.trace)
    try:
        response_value = await FakeProvider([LLMResponse(content="4", usage={"total_tokens": 17})]).chat_with_retry(messages=[])
        assert response_value.content == "4"
    finally:
        active_context_trace.reset(token)
    # A later turn cancellation does not erase a finished provider attempt.
    selected.control.save(lambda state: state.update(instruction_status="CANCELLED", status="CANCELLED"))
    assert selected.trace.calls[-1]["status"] == "succeeded"
    assert selected.trace.calls[-1]["provider_usage"]["total_tokens"] == 17


async def test_real_local_test_execution_is_required_and_bound_to_source_version(env, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text('{"value": 2}')
    test_file = tmp_path / "test_local_contract.py"
    test_file.write_text("import json\nfrom pathlib import Path\ndef test_value():\n    assert json.loads(Path('settings.json').read_text())['value'] == 2\n")
    env[2].register(ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True, path_prepend=str(Path(sys.executable).parent)))
    request = "settings.json 변경 버전에 대한 테스트를 실행해줘"
    event(env, request)
    design = plan(route="execute", request_type="investigate", answer=None, capabilities_needed=["exec"],
        completion_checks=["Tests passed"], completion_specs=[{
            "check": "Tests passed", "result_kind": "test_result", "target": str(target)}])
    _, _, selected = await select(env, design, request=request)
    command = "python -m pytest test_local_contract.py -q"
    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("test-run", "exec", {"command": command, "working_dir": str(tmp_path), "intent_summary": "Run the authorized temporary test",
            "target": str(test_file), "scope": "once", "reversible": True})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("verify", "record_context_completion", {
            "check": "Tests passed", "tool_call_ids": ["test-run"], "status": "passed", "finding": "Local test exited zero against this file version"})]),
        LLMResponse(content="Local test executed and passed.")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": request}], tools=selected.tools, model="offline",
        workspace=tmp_path, max_iterations=4, max_tool_result_chars=4000, selected_context=selected))
    assert result.stop_reason == "completed", [m for m in result.messages if m.get("role") == "tool"]
    evidence = selected.observations["test-run"]
    assert evidence["result_kind"] == "test_result"
    assert evidence["exit_code"] == 0
    assert evidence["checked_versions"][str(target)] == fingerprint(target)
    target.write_text('{"value": 3}')
    invalid = await RecordContextCompletionTool(selected).execute("Tests passed", ["test-run"], "passed", "Old test result")
    assert invalid.is_error


async def test_change_between_prepare_and_dispatch_is_stopped_at_final_boundary(env):
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"]), request="파일 수정해줘")
    queued = []
    original = selected.tools.prepare_call

    def prepare(name, params):
        result = original(name, params)
        if name == "change_test_file":
            queued.append({"role": "user", "content": "취소해줘"})
        return result

    async def drain(**kwargs):
        messages = queued[:]
        queued.clear()
        return messages

    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("change", "change_test_file", {})]),
        LLMResponse(content="중단 요청을 확인했습니다.")])
    with patch.object(selected.tools, "prepare_call", side_effect=prepare):
        result = await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "파일 수정해줘"}], tools=selected.tools,
            model="offline", max_iterations=2, max_tool_result_chars=1000,
            selected_context=selected, injection_callback=drain))
    assert env[2].get("change_test_file").calls == 0
    assert result.stop_reason == "cancelled"
    assert selected.state["cancellations"][0]["timing"] == "before_execution"


async def test_reconciliation_cannot_erase_new_read_only_restriction(env):
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None), request="파일 수정해줘")
    selected.control.ingest_messages([{"role": "user", "content": "이제 읽기만 하세요. 수정하지 마세요."}])
    result = await selected.tools.execute("revise_context_requirements", {
        "reason": "new_user_instruction", "instruction_version": selected.instruction_version,
        "information_needs": [], "completion_checks": []})
    assert not getattr(result, "is_error", False)
    assert selected.control.authorize("change_test_file")
    stale = await selected.tools.execute("revise_context_requirements", {
        "reason": "new_user_instruction", "instruction_version": selected.instruction_version - 1,
        "information_needs": [], "completion_checks": []})
    assert stale.is_error


async def test_code_retrieved_memory_can_be_assessed_without_an_extra_read(env):
    original = event(env, "배포 버전은 2이며 사용자 승인 전 배포 금지입니다.")
    request = "이 설정을 근거로 파일 수정해줘"
    event(env, request)
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        information_needs=[need(query="배포 버전", source_ids=[original.event_id])]), request=request)
    assert not selected.missing
    assert selected.evidence_error()  # acquisition is distinct from assessment
    result = await selected.tools.execute("resolve_context_need", {
        "need_id": "n1", "source_event_ids": [original.event_id],
        "finding": "원문 버전 2와 배포 금지 조건을 유지한다"})
    assert not getattr(result, "is_error", False)
    assert selected.evidence_error() is None
    assert not selected.observations  # no redundant model-requested search/read


async def test_full_loop_real_read_write_verify_and_persistent_state(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ContextSelectionConfig

    target = tmp_path / "settings.json"
    target.write_text('{"value": 1}')
    design = plan(goal="Change settings.json value to 2", route="execute", request_type="modify", answer=None,
        capabilities_needed=["read_file", "write_file"], information_needs=[need(
            source_kind="file", query="settings.json", target="settings.json", expected_values={"value": 1})],
        completion_checks=["Current value is 2"], completion_specs=[{"check": "Current value is 2",
            "result_kind": "file_content", "target": "settings.json", "expected_values": {"value": 2}}])
    provider = FakeProvider([
        response(design),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("before", "read_file", {"path": "settings.json", "force": True})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("assess", "resolve_context_need", {
            "need_id": "n1", "tool_call_id": "before", "finding": "The original value is 1"})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("write", "write_file", {
            "path": "settings.json", "content": '{"value": 2}', "intent_summary": "Apply the requested value",
            "target": "settings.json", "scope": "once", "reversible": True})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("after", "read_file", {"path": "settings.json", "force": True})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("verify", "record_context_completion", {
            "check": "Current value is 2", "tool_call_ids": ["write", "after"], "status": "passed",
            "finding": "The saved file has value 2 at its current hash"})]),
        LLMResponse(content="값을 2로 저장하고 다시 읽어 검증했습니다.")])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True))
    session = loop.sessions.get_or_create("cli:s")
    request = InboundMessage(channel="cli", sender_id="owner", chat_id="s",
                             content="settings.json 원문 value 1을 확인하고 2로 수정한 뒤 다시 읽어 검증해줘")
    with patch.object(session, "get_history", side_effect=AssertionError("no full replay")):
        out = await loop._process_message(request)
    assert "다시 읽어 검증" in out.content
    assert json.loads(target.read_text()) == {"value": 2}
    state = TaskContextStore(loop.conversation_memory).list(loop._memory_scope_for_message(request), "cli:s")[0]
    assert state["status"] == "DONE"
    assert state["completion_assessments"]["Current value is 2"]["status"] == "passed"
    assert state["executions"][0]["status"] == "ok"
    assert state["evidence"]["before"]["version"] != state["evidence"]["after"]["version"]
    before = next(m for m in session.messages if m.get("tool_call_id") == "before")
    assert '"value": 1' in before["content"]
    with loop.conversation_memory.connect() as conn:
        metrics = json.loads(conn.execute("SELECT metrics FROM context_selection_runs").fetchone()[0])
        assert conn.execute("SELECT count(*) FROM context_model_attempts").fetchone()[0] == 7
        assert conn.execute("SELECT count(*) FROM events WHERE event_id=?", (before["memory_event_id"],)).fetchone()[0] == 1
    assert [c["call_sequence"] for c in metrics["calls"]] == list(range(1, 8))
    assert all(c["status"] == "succeeded" for c in metrics["calls"])
    assert "value" not in json.dumps(metrics)


@pytest.mark.parametrize("command", ["pytest --help", "pytest --version", "pytest --collect-only", "echo '1 passed'", "python -c 'print(1)'", "pytest; echo ok"])
def test_help_listing_and_echo_are_not_test_execution(command):
    from nanobot.agent.context_evidence import test_command as actual_test_command

    assert not actual_test_command(command)


@pytest.mark.parametrize("text", ["취소하지 말고 계속 진행해줘", "취소된 작업의 상태를 알려줘", "Do not cancel this task"])
def test_mentions_or_negated_cancellation_do_not_cancel_work(text):
    from nanobot.agent.context_control import instruction_policy

    assert not instruction_policy(text)["cancel"]


async def test_exclusion_alone_does_not_erase_unrelated_unfinished_checks(env):
    event(env, "A와 B의 장단점을 설명해줘")
    _, _, first = await select(env, plan(route="retrieve", answer=None,
        completion_checks=["Explain A", "Explain B"]), request="A와 B의 장단점을 설명해줘")
    event(env, "그 방식은 제외하고 설명해줘")
    _, _, changed = await select(env, plan(route="retrieve", answer=None,
        task_relation="change", task_id=first.state["task_id"], completion_checks=["Use another method"]),
        request="그 방식은 제외하고 설명해줘")
    assert "Explain A" in changed.state["unfinished"]
    assert "Explain B" in changed.state["unfinished"]


async def test_read_only_restriction_from_other_task_is_not_merged(env):
    event(env, "작업 A는 읽기만 하세요")
    _, _, first = await select(env, plan(route="retrieve", answer=None), request="A 설명해줘")
    event(env, "다른 작업 B의 파일을 수정해줘")
    _, _, second = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"]), request="다른 작업 B의 파일을 수정해줘")
    assert first.state["task_id"] != second.state["task_id"]
    assert second.control.authorize("change_test_file") is None
    assert first.control.authorize("change_test_file")


async def test_reused_actual_tool_call_id_does_not_execute_a_second_change(env):
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"]), request="파일 수정해줘")
    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("same-id", "change_test_file", {})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("same-id", "change_test_file", {})]),
        LLMResponse(content="부분 결과를 기록했습니다.")])
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "파일 수정해줘"}], tools=selected.tools,
        model="offline", max_iterations=3, max_tool_result_chars=1000, selected_context=selected))
    assert env[2].get("change_test_file").calls == 1
    assert len(selected.state["executions"]) == 1


async def test_stopping_runner_does_not_falsely_cancel_noninterruptible_effect(env):
    entered = asyncio.Event()
    release = asyncio.Event()
    actual = asyncio.Event()

    class Delayed(ChangeTool):
        async def execute(self, **kwargs):
            entered.set()
            await release.wait()
            actual.set()
            return "late real change"

    env[2].register(Delayed())
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None,
        capabilities_needed=["change_test_file"]), request="파일 수정해줘")
    provider = FakeProvider([LLMResponse(content=None, tool_calls=[ToolCallRequest("late", "change_test_file", {})])])
    runner = asyncio.create_task(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "파일 수정해줘"}], tools=selected.tools,
        model="offline", max_iterations=2, max_tool_result_chars=1000, selected_context=selected)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert selected.state["executions"][0]["status"] == "unknown"
    release.set()
    await asyncio.wait_for(actual.wait(), timeout=1)
    for _ in range(20):
        if selected.state["executions"][0].get("late_result"):
            break
        await asyncio.sleep(0.01)
    assert selected.state["executions"][0]["status"] == "ok"
    assert selected.state["executions"][0]["late_result"]
    assert selected.state["instruction_status"] == "CANCELLED"
    assert selected.observations["late"]["source_event_id"]


async def test_streamed_model_attempt_is_journaled_before_first_delta(env):
    event(env, "2+2?")
    _, _, selected = await select(env, plan())

    async def delta(text):
        with env[0].connect() as conn:
            status = conn.execute("SELECT status FROM context_model_attempts ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert status == "in_flight"

    token = active_context_trace.set(selected.trace)
    try:
        await FakeProvider([LLMResponse(content="4", usage={"total_tokens": 5})]).chat_stream_with_retry(
            messages=[], on_content_delta=delta)
    finally:
        active_context_trace.reset(token)
    assert selected.trace.calls[-1]["status"] == "succeeded"
    assert selected.trace.calls[-1]["provider_usage"]["total_tokens"] == 5


async def test_fallback_journals_leaf_attempts_without_counting_router_twice(env):
    from nanobot.config.schema import ModelPresetConfig
    from nanobot.providers.fallback_provider import FallbackProvider

    event(env, "2+2?")
    _, _, selected = await select(env, plan())
    primary = FakeProvider([LLMResponse(content="server error", finish_reason="error", error_kind="server_error")])
    fallback = FakeProvider([LLMResponse(content="4", usage={"total_tokens": 9})])
    provider = FallbackProvider(primary, [ModelPresetConfig(model="offline-fallback")], lambda config: fallback)
    token = active_context_trace.set(selected.trace)
    before = len(selected.trace.calls)
    try:
        result = await provider.chat_with_retry(messages=[])
    finally:
        active_context_trace.reset(token)
    assert result.content == "4"
    calls = selected.trace.calls[before:]
    assert len(calls) == 2
    assert [c["status"] for c in calls] == ["failed", "succeeded"]
    assert calls[1]["retry_of"] == calls[0]["attempt_id"]


async def test_selected_success_claim_is_not_streamed_before_completion_gate(env):
    from nanobot.agent.hook import AgentHook

    class VisibleHook(AgentHook):
        def __init__(self):
            super().__init__()
            self.deltas = []

        def wants_streaming(self):
            return True

        async def on_stream(self, context, delta):
            self.deltas.append(delta)

    event(env, "설정을 확인하고 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        information_needs=[need(source_kind="file", target="settings.json")]),
        request="설정을 확인하고 수정해줘")
    hook = VisibleHook()
    progress = []

    async def emit(delta):
        progress.append(delta)

    provider = FakeProvider([LLMResponse(content="수정과 검증을 완료했습니다.")])
    provider.supports_progress_deltas = True
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "설정을 확인하고 수정해줘"}],
        tools=selected.tools, model="offline", max_iterations=1, max_tool_result_chars=2000,
        selected_context=selected, hook=hook, progress_callback=emit))
    assert not hook.deltas
    assert not progress
    assert "완료했습니다" not in result.final_content
    assert result.stop_reason == "incomplete"


async def test_semantic_completion_read_becomes_invalid_when_file_changes(env, tmp_path):
    selected, tool = await file_task(env, tmp_path)
    await acquire(selected, tool)
    resolved = await selected.tools.resolver.execute("n1", "read", "Required original acquired")
    assert not getattr(resolved, "is_error", False)
    check = selected.design.completion_checks[0]
    result = await RecordContextCompletionTool(selected).execute(check, ["read"], "passed", "Original checked")
    assert not getattr(result, "is_error", False)
    (tmp_path / "settings.json").write_text('{"value": 404}')
    result = await RecordContextCompletionTool(selected).execute(check, ["read"], "passed", "Still checked")
    assert result.is_error
    assert not selected.completion_supported()
    final, status = selected.verdict("모든 작업을 완료했습니다.")
    assert status == "incomplete"
    assert "완료했습니다" not in final
    assert "completion_evidence_invalid_or_insufficient" in final

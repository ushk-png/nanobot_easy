"""v4.2 counterexamples: scripted models, real local tools, isolated targets."""
import asyncio
import re
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import (
    TaskNote,
    TaskUpdate,
    active_task_note,
    bounded_operation,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config, StepwiseConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager


def target(path="a.md", **kwargs):
    return {"path": path, "selection_reason": "current conversation selects this file", **kwargs}


def update(note, *, targets=None, **kwargs):
    return TaskUpdate.model_validate({
        "request_id": note.request_id, "intent": "new", "goal": note.request,
        "targets": targets if targets is not None else [target()], "source": "files",
        "analysis_complete": True, "status": "completed", "explanation": "analysis performed",
        "final_answer": "검토 결과입니다.",
        "result_refs": [e["id"] for e in note.events if e["mutating"] and e["status"] == "ok"],
        **kwargs,
    })


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        step = next(self.steps)
        return step(kwargs) if callable(step) else step


def control(**changes):
    answer = changes.pop("answer", "")
    def response(kwargs):
        guidance = next(m["content"] for m in reversed(kwargs["messages"])
                        if isinstance(m.get("content"), str) and "current request_id=" in m["content"])
        request_id = re.search(r"current request_id=([a-f0-9]+)", guidance)[1]
        args = {"request_id": request_id, "intent": "new", "goal": "review requested file",
                "targets": [target()], "source": "files", "analysis_complete": True,
                "status": "completed", "explanation": "analysis performed",
                "final_answer": answer or "검토 결과입니다.", **changes}
        return LLMResponse(content=answer, tool_calls=[ToolCallRequest(
            id="control-" + str(len(kwargs["messages"])), name="update_task", arguments=args)])
    return response


def call(name, **args):
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id=name, name=name, arguments=args)])


def final(text="검토 결과: 문제를 확인했습니다."):
    return LLMResponse(content=text, tool_calls=[])


async def run(tmp_path, steps, request="a.md 전체 파일을 검토해 줘", **kwargs):
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(kwargs.pop("write_tool", None) or WriteFileTool(workspace=tmp_path))
    provider = Script(steps)
    metadata = kwargs.pop("task_note_metadata", {})
    spec = AgentRunSpec(initial_messages=[{"role": "user", "content": request}],
                        tools=tools, model="test", max_iterations=12, max_tool_result_chars=8000,
                        workspace=tmp_path, task_note_enabled=True,
                        task_note_metadata=metadata, **kwargs)
    result = await AgentRunner(provider).run(spec)
    return result, provider, metadata


async def read(note, tmp_path, name="a.md", **kwargs):
    tool = ReadFileTool(workspace=tmp_path)
    token = active_task_note.set(note)
    try:
        result = await tool.execute(path=name, force=True, **kwargs)
    finally:
        active_task_note.reset(token)
    note.observe("read_file", {"path": name}, result, tool, "ok")
    return result


def test_t01_old_text_never_confirms_new_intent(tmp_path):
    note = TaskNote([{"role": "assistant", "content": "요청 해석: 계속"},
                     {"role": "user", "content": "저장하지 말고 검토만 해."}], tmp_path, {})
    assert note.defects() == ["intent_unconfirmed"]
    assert note.mutation_error("write_file", {"path": "a.md"})
    stale = update(note)
    note.revise([{"role": "user", "content": "변경하지 마"}])
    assert note.accept(stale).is_error


async def test_t01_pending_followup_prevents_prepared_write(tmp_path):
    injections = AsyncMock(side_effect=[
        [{"role": "user", "content": "저장하지 말고 검토만 해."}], [], [], [], [], [],
    ])
    result, _, metadata = await run(tmp_path, [
        call("write_file", path="a.md", content="unapproved"),
        control(source="none", targets=[], status="clarification", analysis_complete=False,
                explanation="검토할 보고서의 대상을 확인해야 합니다."),
        final("저장하지 않았습니다. 검토할 보고서를 지정해 주세요."),
    ], request="보고서를 수정하고 저장해 줘", injection_callback=injections)
    assert not (tmp_path / "a.md").exists()
    assert result.stop_reason == "incomplete"
    assert result.tool_events[0]["status"] == "superseded"
    assert metadata["task_note"]["intent"] == "new"


async def test_t02_all_targets_required_and_wrong_file_rejected(tmp_path):
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.md").write_text("B", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "그 설계서 두 개를 비교 검토해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(), target("b.md")]))
    await read(note, tmp_path)
    note.accept(update(note, targets=[target(), target("b.md")]))
    assert note.defects() == ["coverage_unconfirmed:b.md"]
    await read(note, tmp_path, "b.md")
    note.accept(update(note, targets=[target(), target("b.md")]))
    assert note.defects() == []


async def test_t03_ambiguous_then_user_selects(tmp_path):
    (tmp_path / "b.md").write_text("selected design", encoding="utf-8")
    result, _, _ = await run(tmp_path, [
        control(targets=[], status="clarification", analysis_complete=False,
                explanation="동명 설계서 두 개 중 어떤 버전인지 필요합니다."),
        final("설계서의 1판과 2판 중 어느 것을 검토할까요?"),
    ], request="설계서 파일을 검토해 줘")
    assert result.stop_reason == "incomplete"
    result, _, _ = await run(tmp_path, [control(targets=[target("b.md")]),
                                        call("read_file", path="b.md"),
                                        control(targets=[target("b.md")], answer="검토 결과입니다.")],
                            request="2판 b.md 파일을 검토해 줘")
    assert result.stop_reason == "completed"


async def test_t04_unknown_contract_and_whole_word_do_not_prove_coverage(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 전체를 검토해 줘"}], tmp_path, {})
    note.accept(update(note))
    note.observe("read_file", {"path": "a.md"}, "1| 전체 시스템의 첫 페이지", object(), "ok")
    note.accept(update(note))
    assert note.defects() == ["coverage_unconfirmed:a.md"]


async def test_t05_full_read_and_requested_range(tmp_path):
    (tmp_path / "a.md").write_text("\n".join(f"line {n}" for n in range(1, 201)), encoding="utf-8")
    result, provider, _ = await run(tmp_path, [
        control(), call("read_file", path="a.md", limit=300), control(answer="검토 결과입니다."),
    ])
    assert result.stop_reason == "completed"
    assert len(provider.calls) == 3
    note = TaskNote([{"role": "user", "content": "a.md 2절만 검토해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(unit="lines", start=50, end=80)]))
    await read(note, tmp_path, offset=1, limit=10)
    assert note.defects()
    await read(note, tmp_path, offset=50, limit=31)
    note.accept(update(note, targets=[target(unit="lines", start=50, end=80)]))
    assert note.defects() == []


async def test_t05_multiple_partial_reads_cover_whole(tmp_path):
    (tmp_path / "a.md").write_text("a\nb\nc\nd", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "a.md 전체 검토"}], tmp_path, {})
    note.accept(update(note))
    await read(note, tmp_path, offset=1, limit=2)
    assert note.defects()
    await read(note, tmp_path, offset=3, limit=2)
    note.accept(update(note))
    assert note.defects() == []


async def test_t06_cancellation_settles_child_and_never_changes_new_note(tmp_path):
    metadata = {}
    note = TaskNote([{"role": "user", "content": "파일 수정"}], tmp_path, metadata)
    started, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def delayed():
        started.set()
        await release.wait()
        effects.append("late change")

    parent = asyncio.create_task(bounded_operation(note, "delayed", delayed))
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    next_note = TaskNote([{"role": "user", "content": "다른 파일 검토"}], tmp_path, metadata)
    release.set()
    await asyncio.sleep(0)
    assert effects == []
    assert note.attempts[0]["cleanup"] == "settled"
    assert metadata["task_note"]["run_id"] == next_note.run_id


async def test_t07_delivery_failure_then_redelivery_never_repeats_save(tmp_path):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("telegram:test")
    session.metadata["task_note"] = {"run_id": "run", "status": "completed"}
    config = Config.model_validate({"agents": {"defaults": {"stepwise": {"lightweight": True}}},
                                    "channels": {"websocket": {"enabled": False}, "sendMaxRetries": 1}})
    manager = ChannelManager(config, MessageBus(), session_manager=sessions)
    channel = type("Channel", (), {"send": AsyncMock(side_effect=RuntimeError("delivery failed"))})()
    msg = OutboundMessage(channel="telegram", chat_id="test", content="saved",
                          metadata={"_task_delivery": {"session_key": session.key, "run_id": "run"}})
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["status"] == "completed"
    assert session.metadata["task_note"]["delivery"] == "failed"
    channel.send = AsyncMock()
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["delivery"] == "sent"
    assert sessions.get_or_create(session.key).metadata["task_deliveries"]["run"]["attempts"] == 1


def test_t08_denied_and_unknown_writes_differ(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 저장해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(action="write")]))
    tool = type("Writer", (), {"read_only": False})()
    note.observe("write_file", {"path": "a.md"}, "permission denied", tool, "error")
    note.accept(update(note, targets=[target(action="write")]))
    assert note.defects() == ["blocked:a.md"]
    note.events.clear()
    note.observe("write_file", {"path": "a.md"}, "timeout", tool, "error")
    note.accept(update(note, targets=[target(action="write")]))
    assert note.defects() == ["record_check:a.md"]
    assert note.mutation_error("write_file", {"path": "a.md"})


async def test_t09_multiple_defects_and_no_repeat_of_successful_write(tmp_path):
    (tmp_path / "a.md").write_text("source", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "a.md 검토하고 b.md 저장해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(), target("b.md", action="write")], analysis_complete=False))
    assert set(note.defects()) == {"coverage_unconfirmed:a.md", "execution_required:b.md", "analysis_incomplete"}
    note.observe("write_file", {"path": "b.md"}, "saved", type("Writer", (), {"read_only": False})(), "ok")
    assert note.mutation_error("write_file", {"path": "b.md"})
    await read(note, tmp_path)
    note.accept(update(note, targets=[target(), target("b.md", action="write")]))
    assert note.defects() == []


async def test_t10_pasted_body_and_valid_cached_read(tmp_path):
    result, _, _ = await run(tmp_path, [control(source="provided", targets=[]), final()],
                            request="이 본문에서 문제점을 검토해 줘.\n```supplied text``` ")
    assert result.stop_reason == "completed"
    (tmp_path / "a.md").write_text("source", encoding="utf-8")
    metadata = {}
    note = TaskNote([{"role": "user", "content": "a.md 검토"}], tmp_path, metadata)
    await read(note, tmp_path)
    cached = TaskNote([{"role": "user", "content": "a.md 다시 검토"}], tmp_path, metadata)
    cached.accept(update(cached))
    assert cached.defects() == []
    (tmp_path / "a.md").write_text("changed", encoding="utf-8")
    assert cached.defects()


async def test_t11_budget_exhaustion_cannot_report_success(tmp_path):
    tools = ToolRegistry()
    provider = Script([final("전체 파일 검토 완료"), final("검토 완료")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 전체 검토"}],
        tools=tools, model="test", max_iterations=1, max_tool_result_chars=8000,
        task_note_enabled=True, task_note_metadata={}, workspace=tmp_path))
    assert result.stop_reason == "incomplete"
    assert "완료하지 못했습니다" in result.final_content


def test_t12_current_intent_outranks_memory_and_old_approval(tmp_path):
    messages = [{"role": "user", "content": "a.md 저장해 줘"},
                {"role": "assistant", "content": "요청 해석: 계속"},
                {"role": "user", "content": "저장하지 말고 검토만 해."}]
    note = TaskNote(messages, tmp_path, {})
    note.accept(update(note, intent="change", source="none", targets=[]))
    assert note.mutation_error("write_file", {"path": "a.md"})


async def test_simple_and_other_agents_have_no_control_tool_or_extra_call(tmp_path):
    result, provider, metadata = await run(tmp_path, [final("2")], request="1+1은?")
    assert len(provider.calls) == 1
    assert not any(d["function"]["name"] == "update_task" for d in provider.calls[0]["tools"])
    assert metadata == {}
    assert StepwiseConfig().lightweight is False
    assert result.final_content == "2"


def test_reference_code_block_does_not_replace_requested_file(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 파일 전체 검토. 참고 ```example```"}], tmp_path, {})
    note.accept(update(note, source="provided", targets=[]))
    assert "target_unresolved" in note.defects()


class Calculation(Tool):
    name = "calculate"
    description = "test calculation"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, **kwargs):
        return ToolResult("4")


async def test_t11_free_tool_switch_with_real_file(tmp_path):
    (tmp_path / "a.md").write_text("2+2", encoding="utf-8")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(Calculation())
    result = await AgentRunner(Script([control(), call("read_file", path="a.md"),
                                       call("calculate"), control(answer="계산 결과는 4입니다.")])).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 파일 분석"}], tools=tools,
        model="test", max_iterations=8, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "completed"
    assert result.tools_used == ["update_task", "read_file", "calculate", "update_task"]


def write_args(content="saved content"):
    return {"path": "a.md", "content": content, "intent_summary": "save requested report",
            "target": "a.md", "scope": "once", "reversible": True}


async def test_t07_real_save_and_failed_send_preserve_execution(tmp_path):
    result, provider, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()),
        control(targets=[target(action="write")], result_refs=["write_file"], answer="a.md에 저장했습니다."),
    ], request="a.md에 보고서를 저장해 줘")
    assert result.stop_reason == "completed"
    assert (tmp_path / "a.md").read_text() == "saved content"
    stamp = (tmp_path / "a.md").stat().st_mtime_ns
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("telegram:save-test")
    session.metadata.update(metadata)
    config = Config.model_validate({"agents": {"defaults": {"stepwise": {"lightweight": True}}},
                                    "channels": {"websocket": {"enabled": False}, "sendMaxRetries": 1}})
    manager = ChannelManager(config, MessageBus(), session_manager=sessions)
    channel = type("Channel", (), {"send": AsyncMock(side_effect=RuntimeError("failed"))})()
    msg = OutboundMessage(channel="telegram", chat_id="save-test", content=result.final_content,
                          metadata={"_task_delivery": {"session_key": session.key,
                                                       "run_id": metadata["task_note"]["run_id"]}})
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["status"] == "completed"
    assert session.metadata["task_note"]["delivery"] == "failed"
    channel.send = AsyncMock()
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["delivery"] == "sent"
    assert (tmp_path / "a.md").stat().st_mtime_ns == stamp
    assert len(provider.calls) == 3
    assert result.tools_used.count("write_file") == 1


class SaveThenTimeout(WriteFileTool):
    async def execute(self, **kwargs):
        await super().execute(**kwargs)
        raise TimeoutError("write response timed out after saving")


async def test_t08_real_saved_timeout_reconciled_by_read_without_second_save(tmp_path):
    result, _, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()), call("read_file", path="a.md"),
        control(targets=[target(action="write")], result_refs=["write_file"], answer="저장 결과를 확인했습니다."),
    ], request="a.md에 보고서를 저장해 줘", write_tool=SaveThenTimeout(workspace=tmp_path))
    assert result.stop_reason == "completed"
    events = metadata["task_note"]["events"]
    writes = [e for e in events if e["name"] == "write_file"]
    assert len(writes) == 1
    assert writes[0]["verification"] == "read_after_unknown_write"


async def test_t09_only_missing_result_reference_is_repaired(tmp_path):
    result, provider, _ = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()),
        control(targets=[target(action="write")], answer="저장했습니다."),
        control(targets=[target(action="write")], result_refs=["write_file"], answer="a.md 저장을 확인했습니다."),
    ], request="a.md 저장해 줘")
    assert result.stop_reason == "completed"
    assert result.tools_used.count("write_file") == 1
    assert len(provider.calls) == 4
    assert any("answer_correction" in str(m.get("content")) for m in result.messages)


async def test_t06_noncooperative_late_result_is_audit_only(tmp_path):
    metadata = {}
    note = TaskNote([{"role": "user", "content": "파일 수정"}], tmp_path, metadata)
    started, release = asyncio.Event(), asyncio.Event()

    async def noncooperative():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return "external operation already completed"

    parent = asyncio.create_task(bounded_operation(note, "remote", noncooperative, grace=0.01))
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    next_note = TaskNote([{"role": "user", "content": "다른 파일 검토"}], tmp_path, metadata)
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert metadata["task_note"]["run_id"] == next_note.run_id
    audit = metadata["task_runs"][note.run_id]
    assert audit["status"] == "cancelled"
    assert audit["attempts"][0]["late_result"] == "returned_after_cancellation"
    assert next_note.events == []


async def test_search_worker_matches_real_search_and_retains_parameters(tmp_path):
    from nanobot.agent.tools.search import FindFilesTool, GrepTool
    from nanobot.agent.tools.search_worker import isolated_search

    (tmp_path / "a.md").write_text("alpha\nbeta", encoding="utf-8")
    (tmp_path / "b.py").write_text("alpha", encoding="utf-8")
    for tool, params in [
        (FindFilesTool(workspace=tmp_path), {"query": "a", "glob": "*.md", "head_limit": 1}),
        (GrepTool(workspace=tmp_path), {"pattern": "alpha", "glob": "*.md", "output_mode": "content"}),
    ]:
        expected = await tool.execute(**params)
        observed = await isolated_search(tool, params)
        assert observed == expected


async def test_model_context_offload_is_not_full_acquisition(tmp_path):
    (tmp_path / "a.md").write_text("x" * 2000, encoding="utf-8")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    result = await AgentRunner(Script([
        control(), call("read_file", path="a.md"), control(answer="전체 검토 완료"),
        control(status="partial", explanation="한 줄이 문맥 한도를 초과해 전체 내용을 확인하지 못했습니다.",
                answer="자료 일부만 확인했습니다."),
    ])).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 전체 검토"}], tools=tools,
        model="test", max_iterations=8, max_tool_result_chars=500, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "incomplete"


async def test_pdf_page_contract_and_partial_page_request(tmp_path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {n + 1}")
    doc.save(tmp_path / "a.pdf")
    doc.close()
    note = TaskNote([{"role": "user", "content": "a.pdf 2페이지만 검토"}], tmp_path, {})
    note.accept(update(note, targets=[target("a.pdf", unit="pages", start=2, end=2)]))
    await read(note, tmp_path, "a.pdf", pages="1")
    note.accept(update(note, targets=[target("a.pdf", unit="pages", start=2, end=2)]))
    assert note.defects()
    await read(note, tmp_path, "a.pdf", pages="2")
    note.accept(update(note, targets=[target("a.pdf", unit="pages", start=2, end=2)]))
    assert note.defects() == []


async def test_disabled_agent_does_not_gain_task_tools(tmp_path):
    tools = ToolRegistry()
    provider = Script([final("평소 답변")])
    metadata = {}
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "파일을 검토해 줘"}], tools=tools,
        model="test", max_iterations=3, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=False, task_note_metadata=metadata))
    assert result.final_content == "평소 답변"
    assert metadata == {}
    assert provider.calls[0]["tools"] == []
    assert not tools.has("update_task")


def test_named_document_cannot_be_reclassified_as_no_source(tmp_path):
    note = TaskNote([{"role": "user", "content": "그 설계서 파일을 검토해 줘"}], tmp_path, {})
    note.accept(update(note, source="none", targets=[]))
    assert "target_unresolved" in note.defects()


async def test_unicode_filename_with_spaces_is_linked(tmp_path):
    name = "설계서 최종.md"
    (tmp_path / name).write_text("actual design", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": f"'{name}'를 검토해 줘"}], tmp_path, {})
    await read(note, tmp_path, name)
    note.accept(update(note, targets=[target(str(tmp_path / name))]))
    assert note.defects() == []


class DeniedWrite(WriteFileTool):
    async def execute(self, **kwargs):
        return ToolResult.error("permission denied before saving")


async def test_t08_permission_denial_finishes_blocked_without_retry(tmp_path):
    result, _, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()),
        control(targets=[target(action="write")], status="blocked", analysis_complete=False,
                explanation="권한이 거부되어 저장하지 못했습니다.", answer="저장 권한이 없습니다."),
    ], request="a.md 저장해 줘", write_tool=DeniedWrite(workspace=tmp_path))
    assert result.stop_reason == "incomplete"
    assert not (tmp_path / "a.md").exists()
    assert len([e for e in metadata["task_note"]["events"] if e["name"] == "write_file"]) == 1


async def test_search_child_is_killed_on_cancellation(tmp_path, monkeypatch):
    import sys

    from nanobot.agent.tools import search_worker
    from nanobot.agent.tools.search import FindFilesTool

    create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []

    async def delayed_worker(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(search_worker.asyncio, "create_subprocess_exec", delayed_worker)
    task = asyncio.create_task(search_worker.isolated_search(FindFilesTool(workspace=tmp_path), {}))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None


async def test_candidate_completion_text_is_not_progress(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.agent.progress_hook import AgentProgressHook

    callback = AsyncMock()
    hook = AgentProgressHook(on_progress=callback)
    note = TaskNote([{"role": "user", "content": "파일 검토"}], tmp_path, {})
    token = active_task_note.set(note)
    try:
        await hook.before_execute_tools(AgentHookContext(
            iteration=0, messages=[], response=LLMResponse(content="검토 완료했습니다."), tool_calls=[]))
    finally:
        active_task_note.reset(token)
    assert all("검토 완료" not in str(call) for call in callback.await_args_list)


async def test_t01_followup_between_read_and_write_in_same_response(tmp_path):
    (tmp_path / "a.md").write_text("original", encoding="utf-8")

    def prepared_actions(kwargs):
        response = control(targets=[target(), target(action="write")],
                           status="active", analysis_complete=False)(kwargs)
        response.tool_calls.extend([
            ToolCallRequest(id="read", name="read_file", arguments={"path": "a.md"}),
            ToolCallRequest(id="write", name="write_file", arguments=write_args("obsolete")),
        ])
        return response

    followups = AsyncMock(side_effect=[[], [], [], [
        {"role": "user", "content": "a.md 저장하지 말고 검토만 해."},
    ], *([[]] * 12)])
    result, _, _ = await run(tmp_path, [
        prepared_actions, control(intent="change", answer="변경 없이 원문을 검토했습니다."),
    ], request="a.md 파일을 검토하고 수정해 저장해 줘", injection_callback=followups,
                            concurrent_tools=True)
    assert result.stop_reason == "completed"
    assert (tmp_path / "a.md").read_text() == "original"
    assert next(e for e in result.tool_events if e["name"] == "write_file")["status"] == "superseded"


async def test_t12_retrieved_memory_and_skill_paths_are_not_new_instructions(tmp_path):
    from nanobot.agent.context import ContextBuilder

    request = ("1+1은?\n\n" + ContextBuilder._RECENT_MEMORY_TAG
               + "\n과거 요청: report.md를 저장해 줘.\n" + ContextBuilder._RECENT_MEMORY_END
               + "\n\n" + ContextBuilder._RUNTIME_CONTEXT_TAG
               + "\nSkill: /somewhere/SKILL.md; 검토 및 파일 수정\n" + ContextBuilder._RUNTIME_CONTEXT_END)
    result, provider, metadata = await run(tmp_path, [final("2")], request=request)
    assert result.final_content == "2"
    assert len(provider.calls) == 1
    assert metadata == {}


def test_old_string_evidence_is_not_promoted_or_reused(tmp_path):
    metadata = {"task_note": {"evidence": ["read_file:ok:a.md"]}}
    note = TaskNote([{"role": "user", "content": "a.md 검토"}], tmp_path, metadata)
    assert note.evidence == []
    assert note.defects() == ["intent_unconfirmed"]


def test_metadata_only_versions_cannot_merge_distinct_reads(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 전체 검토"}], tmp_path, {})
    note.accept(update(note))
    note.evidence = [{"target": str(tmp_path / "a.md"), "unit": "lines", "start": start,
                      "end": end, "total": 10, "extraction_ok": True,
                      "request_id": note.request_id, "version": {"size": 2_000_000, "mtime_ns": 1}}
                     for start, end in [(1, 5), (6, 10)]]
    assert note.defects() == ["coverage_unconfirmed:a.md"]


def test_distinct_authorized_edits_are_not_blocked_as_delivery_duplicates(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 수정해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(action="write")]))
    tool = type("Writer", (), {"read_only": False})()
    params = {"path": "a.md", "old_text": "old", "new_text": "new"}
    note.observe("edit_file", params, "saved", tool, "ok")
    assert note.mutation_error("edit_file", {**params, "path": "./a.md"})
    assert note.mutation_error("edit_file", {**params, "old_text": "another"}) is None


async def test_analysis_complete_without_public_answer_is_not_completion(tmp_path):
    (tmp_path / "a.md").write_text("The same start operation ran three times.", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "a.md 원인 분석"}], tmp_path, {})
    await read(note, tmp_path)
    note.accept(update(note, explanation="start was repeated three times", final_answer=""))
    assert "answer_required" in note.defects()


async def test_observed_closure_notice_recovers_report_without_rereading(tmp_path):
    (tmp_path / "a.md").write_text("06:52 start\n06:53 start\n06:54 cancelled", encoding="utf-8")
    report = "원인은 초기화 뒤 start를 반복 호출한 것입니다. 로그에 두 번의 시작과 마지막 취소가 기록돼 있습니다."
    result, provider, _ = await run(tmp_path, [
        call("read_file", path="a.md"),
        control(answer="필요한 구간은 모두 확인됐습니다. 원인 판정을 닫겠습니다.",
                explanation="Repeated start calls were found", final_answer=""),
        control(answer=report, final_answer=report),
    ])
    assert result.final_content == report
    assert result.tools_used.count("read_file") == 1
    assert len(provider.calls) == 3


async def test_structured_answer_is_delivered_instead_of_status_sentence(tmp_path):
    (tmp_path / "a.md").write_text("duplicate trigger", encoding="utf-8")
    report = "원인은 같은 트리거가 중복 실행된 것입니다. 자료에서 중복 호출을 확인했습니다."
    result, provider, _ = await run(tmp_path, [
        call("read_file", path="a.md"),
        control(answer="판정에 필요한 기록은 이미 확인됐습니다. 그 범위로 결론을 닫겠습니다.",
                final_answer=report),
    ])
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert len(provider.calls) == 2


async def test_continue_analysis_reuses_evidence_but_requires_actual_report(tmp_path):
    (tmp_path / "a.md").write_text("start repeated twice", encoding="utf-8")
    metadata = {}
    old_note = TaskNote([{"role": "user", "content": "a.md 원인을 파악해 줘"}], tmp_path, metadata)
    await read(old_note, tmp_path)
    report = "기록상 start를 두 번 호출해 반복됐습니다. 추가 전송 여부는 별도 확인이 필요합니다."
    result, provider, _ = await run(tmp_path, [
        control(intent="continue", answer="판정에 필요한 기록은 이미 확인됐습니다. 그 범위로 결론을 닫겠습니다.",
                final_answer=""),
        control(intent="continue", final_answer=report),
    ], request="판정을 계속해 줘", task_note_metadata=metadata)
    assert result.final_content == report
    assert result.tools_used == ["update_task", "update_task"]
    assert len(provider.calls) == 2


async def test_reporting_recovery_still_allowed_after_coverage_recovery(tmp_path):
    (tmp_path / "a.md").write_text("start repeated twice", encoding="utf-8")
    report = "원인은 같은 시작 작업의 중복 호출입니다. 로그에 호출 두 건이 있습니다."
    result, provider, _ = await run(tmp_path, [
        control(answer="검토 완료", final_answer=report),
        call("read_file", path="a.md"),
        control(answer="원인 판정을 닫겠습니다.", final_answer=""),
        control(final_answer=report),
    ])
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert result.tools_used.count("read_file") == 1
    assert len(provider.calls) == 4


async def test_reporting_recovery_cannot_repeat_mutation(tmp_path):
    report = "기존 자료만으로 원인은 아직 확정할 수 없습니다. 파일 변경은 수행하지 않았습니다."
    result, _, _ = await run(tmp_path, [
        control(source="provided", targets=[], answer="분석을 마무리하겠습니다.", final_answer=""),
        call("write_file", **write_args()),
        control(source="provided", targets=[], final_answer=report),
    ], request="제공된 본문을 분석해 줘.\n```sample text``` ")
    assert result.final_content == report
    assert not (tmp_path / "a.md").exists()
    assert next(e for e in result.tool_events if e["name"] == "write_file")["status"] == "blocked"


@pytest.mark.parametrize("answer", [
    "원인 판정을 닫겠습니다.",
    "필요한 구간은 모두 확인됐습니다. 원인 판정을 닫겠습니다.",
    "판정에 필요한 기록은 이미 확인됐습니다. 그 범위로 결론을 닫겠습니다.",
    "먼저 policy.py를 읽고 판정을 닫겠습니다.",
])
def test_closing_promises_are_not_a_public_report(tmp_path, answer):
    note = TaskNote([{"role": "user", "content": "이 본문 원인을 분석해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[], source="provided", final_answer=answer))
    assert note.defects() == ["answer_required"]


async def test_last_iteration_still_delivers_report_without_extra_model_call(tmp_path):
    (tmp_path / "a.md").write_text("two starts", encoding="utf-8")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    report = "로그에서 시작 호출 두 건을 확인했습니다. 중복 시작이 원인입니다."

    def last_response(kwargs):
        response = control(final_answer=report)(kwargs)
        response.tool_calls.insert(0, ToolCallRequest(id="read", name="read_file", arguments={"path": "a.md"}))
        return response

    provider = Script([last_response])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 원인 분석"}], tools=tools,
        model="test", max_iterations=1, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert len(provider.calls) == 1


async def test_no_budget_cannot_complete_without_report(tmp_path):
    tools = ToolRegistry()
    provider = Script([control(source="provided", targets=[], final_answer="", answer="판정을 닫겠습니다.")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "아래 본문의 원인을 분석해 줘\n```records```"}],
        tools=tools, model="test", max_iterations=1, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "incomplete"
    assert "answer_required" in result.final_content
    assert len(provider.calls) == 1


async def test_structured_report_still_passes_existing_public_content_filter(tmp_path):
    from nanobot.agent.progress_hook import AgentProgressHook

    result, _, metadata = await run(tmp_path, [
        control(source="provided", targets=[], final_answer="<think>private reasoning</think>원인은 중복 호출입니다."),
    ], request="제공된 본문을 분석해 줘\n```duplicate calls```", hook=AgentProgressHook())
    assert result.final_content == "원인은 중복 호출입니다."
    assert "private reasoning" not in metadata["task_note"]["update"]["final_answer"]

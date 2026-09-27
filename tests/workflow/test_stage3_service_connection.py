from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.context import runtime_lines
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext, ToolContext, bind_request_context, reset_request_context
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.workflow import WorkflowTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config, ToolsConfig
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager
from nanobot.workflow.control import handle_workflow_runtime_control
from nanobot.workflow.delivery import WorkflowDeliveryHook, clear_workflow_turn_delivery
from nanobot.workflow.schema import WorkflowDefinition, WorkflowEnvelope, WorkflowPrincipal
from nanobot.workflow.service import WorkflowService


class AgentWorkflowProvider(LLMProvider):
    def __init__(self, workflow_script: list[dict[str, Any]]):
        super().__init__()
        self.workflow_script = list(workflow_script)
        self.workflow_payloads: list[dict[str, Any]] = []
        self.agent_calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        if tools is not None:
            self.agent_calls += 1
            if self.agent_calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(
                        id="call_workflow_1",
                        name="workflow",
                        arguments={"action": "run", "input": "LLM이 바꿔 쓴 사용자 요청"},
                    )],
                )
            return LLMResponse(content="agent final")
        if not self.workflow_script:
            raise AssertionError("workflow script exhausted")
        payload = json.loads(messages[-1]["content"])
        self.workflow_payloads.append(payload)
        return LLMResponse(content=json.dumps(self.workflow_script.pop(0), ensure_ascii=False))

    def get_default_model(self) -> str:
        return "scripted"


class ScriptedProvider(LLMProvider):
    def __init__(self, script: list[dict[str, Any]]):
        super().__init__()
        self.script = list(script)
        self.step_ids: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        if not self.script:
            raise AssertionError("script exhausted")
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        self.step_ids.append(str(payload.get("step_id")))
        return LLMResponse(content=json.dumps(self.script.pop(0), ensure_ascii=False))

    def get_default_model(self) -> str:
        return "scripted"


def load_definition() -> WorkflowDefinition:
    path = Path("nanobot/workflow/definitions/situation_judgment.v1.json")
    return WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))


def principal(tmp_path: Path) -> WorkflowPrincipal:
    return WorkflowPrincipal(
        workspace=str(tmp_path.resolve()),
        session_key="telegram:1",
        channel="telegram",
        chat_id="1",
    )


def test_workflow_tool_is_hidden_from_subagent_scope(tmp_path: Path) -> None:
    registry = ToolRegistry()
    ctx = ToolContext(config=ToolsConfig(workflow={"enabled": True}), workspace=str(tmp_path))

    ToolLoader().load(ctx, registry, scope="subagent")

    assert not registry.has("workflow")


@pytest.mark.asyncio
async def test_workflow_tool_rejects_mutating_actions_without_session_key(tmp_path: Path) -> None:
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: ScriptedProvider([]), bus=MessageBus())
    ctx = ToolContext(
        config=ToolsConfig(workflow={"enabled": True}),
        workspace=str(tmp_path),
        workflow_service=service,
        workflow_registry=ToolRegistry(),
    )
    token = bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key=""))
    try:
        tool = WorkflowTool.create(ctx)
        for action in ("run", "resume", "status", "cancel"):
            result = json.loads(await tool.execute(action=action, input="hello", task_id="wf_1"))
            assert result["state"] == "NEEDS_ATTENTION"
            assert "requires a non-empty session_key" in result["reason"]
    finally:
        reset_request_context(token)


@pytest.mark.asyncio
async def test_workflow_tool_allows_list_without_session_key(tmp_path: Path) -> None:
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: ScriptedProvider([]), bus=MessageBus())
    ctx = ToolContext(config=ToolsConfig(workflow={"enabled": True}), workspace=str(tmp_path), workflow_service=service)
    token = bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key=""))
    try:
        tool = WorkflowTool.create(ctx)
        result = json.loads(await tool.execute(action="list"))
    finally:
        reset_request_context(token)

    assert result[0]["id"] == "situation_judgment.v1"


@pytest.mark.asyncio
async def test_workflow_service_start_marks_expired_running_lease_needs_attention(tmp_path: Path) -> None:
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: ScriptedProvider([]), bus=MessageBus())
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_running",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_running", state="RUNNING", reason="in progress"),
        context={"user_text": "질문"},
        data={"results": {"external_tool": {"status": "maybe_done"}}},
        trace=[{"step": "external_tool", "type": "tool"}],
    )
    service.store.set_lease("wf_running", lease_until=time.time() - 10)

    await service.start()

    row = service.store.get_task("wf_running")
    assert row is not None
    assert row["state"] == "NEEDS_ATTENTION"
    assert row["data"]["results"]["external_tool"] == {"status": "maybe_done"}
    assert row["trace"] == [{"step": "external_tool", "type": "tool"}]
    envelope = WorkflowEnvelope.model_validate(row["envelope"])
    assert "expired running lease was not automatically replayed" in (envelope.reason or "")
    assert "Inspect prior trace/data" in (envelope.next_hint or "")


@pytest.mark.asyncio
async def test_workflow_service_start_leaves_active_or_waiting_tasks_unchanged(tmp_path: Path) -> None:
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: ScriptedProvider([]), bus=MessageBus())
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_active",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_active", state="RUNNING", reason="in progress"),
        context={},
    )
    service.store.set_lease("wf_active", lease_until=time.time() + 300)
    service.store.upsert_task(
        task_id="wf_waiting",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_waiting", state="WAITING_USER", question="확인?", question_id="q1"),
        context={},
        question_id="q1",
    )
    service.store.set_lease("wf_waiting", lease_until=time.time() - 10)

    await service.start()

    assert service.store.get_task("wf_active")["state"] == "RUNNING"  # type: ignore[index]
    assert service.store.get_task("wf_waiting")["state"] == "WAITING_USER"  # type: ignore[index]


@pytest.mark.asyncio
async def test_workflow_service_persists_waiting_and_resume_access(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"ack": True},
        {"action": "ask_user", "reason": "need preference", "question": "선호?"},
        {"action": "answer", "reason": "preference received"},
        {"answer": "검토된 최종 답변"},
        {"decision": "pass", "reason": "ok"},
    ])
    service = WorkflowService(
        workspace=tmp_path,
        provider_loader=lambda: provider,
        bus=MessageBus(),
        sync_wait_seconds=60,
    )
    envelope = await service.run(
        principal=principal(tmp_path),
        user_text="질문",
        registry=ToolRegistry(),
    )
    assert envelope.state == "WAITING_USER"
    assert envelope.question == "선호?"
    rows = service.waiting_tasks_for_principal(principal(tmp_path))
    assert rows and rows[0]["task_id"] == envelope.task_id

    other = WorkflowPrincipal(workspace=str(tmp_path.resolve()), session_key="telegram:2", channel="telegram", chat_id="2")
    denied = service.status(principal=other, task_id=envelope.task_id)
    assert not isinstance(denied, list)
    assert denied.state == "NEEDS_ATTENTION"

    resumed = await service.resume(
        principal=principal(tmp_path),
        task_id=envelope.task_id or "",
        question_id=envelope.question_id,
        answer="A",
        registry=ToolRegistry(),
    )
    assert resumed.state == "COMPLETED"
    assert resumed.reason == "검토된 최종 답변"
    assert resumed.reason != "A"
    assert provider.step_ids == [
        "receive_context",
        "judge_sufficiency",
        "judge_sufficiency",
        "draft_answer",
        "review_answer",
    ]
    stored = service.store.get_task(envelope.task_id or "")
    assert stored is not None
    assert stored["data"]["results"]["user_answer"]["answer"] == "A"
    assert stored["data"]["results"]["review_answer"]["decision"] == "pass"


@pytest.mark.asyncio
async def test_workflow_tool_resume_uses_session_answer_not_llm_input(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"ack": True},
        {"action": "ask_user", "reason": "need preference", "question": "선호?"},
        {"action": "answer", "reason": "원래 답변으로 충분"},
        {"answer": "원래 답변 기준 최종"},
        {"decision": "pass", "reason": "ok"},
    ])
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("telegram:1")
    session.add_message("user", "원래 사용자 답변", message_id="m-answer")
    sessions.save(session)
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: provider, bus=MessageBus())
    start = await service.run(principal=principal(tmp_path), user_text="질문", registry=ToolRegistry())
    assert start.state == "WAITING_USER"
    ctx = ToolContext(
        config=ToolsConfig(workflow={"enabled": True}),
        workspace=str(tmp_path),
        sessions=sessions,
        workflow_service=service,
        workflow_registry=ToolRegistry(),
    )
    token = bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key="telegram:1", message_id="m-answer"))
    try:
        tool = WorkflowTool.create(ctx)
        resumed = json.loads(await tool.execute(
            action="resume",
            task_id=start.task_id,
            question_id=start.question_id,
            input="LLM이 바꿔 넣은 답변",
        ))
    finally:
        reset_request_context(token)

    assert resumed["state"] == "COMPLETED"
    row = service.store.get_task(start.task_id or "")
    assert row is not None
    answer = row["data"]["results"]["user_answer"]
    assert answer["answer"] == "원래 사용자 답변"
    assert answer["source"] == "message_id"
    assert row["data"]["results"]["resume_input_reference"]["input"] == "LLM이 바꿔 넣은 답변"
    resume_payload = provider.payloads[2]
    assert resume_payload["data"]["results"]["user_answer"]["answer"] == "원래 사용자 답변"


@pytest.mark.asyncio
async def test_resume_derives_next_from_wait_user_step_when_resume_next_missing(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"action": "answer", "reason": "answered"},
        {"answer": "완료"},
        {"decision": "pass", "reason": "ok"},
    ])
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: provider, bus=MessageBus())
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_wait",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_wait", state="WAITING_USER", question="확인?", question_id="q1", deliver="question"),
        context={"user_text": "질문"},
        data={"results": {"user_question": {"question_id": "q1", "question": "확인?"}}},
        question_id="q1",
        resume_next=None,
    )

    resumed = await service.resume(principal=p, task_id="wf_wait", question_id="q1", answer="A", registry=ToolRegistry())

    assert resumed.state == "COMPLETED"
    assert provider.step_ids == ["judge_sufficiency", "draft_answer", "review_answer"]


@pytest.mark.asyncio
async def test_resume_returns_needs_attention_when_wait_user_next_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    definition = load_definition().model_copy(deep=True)
    wait = next(step for step in definition.steps if step.id == "ask_user")
    wait.next = None
    monkeypatch.setattr(WorkflowService, "_load_definition", staticmethod(lambda _definition_id: definition))
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: ScriptedProvider([]), bus=MessageBus())
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_wait",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_wait", state="WAITING_USER", question="확인?", question_id="q1", deliver="question"),
        context={"user_text": "질문"},
        data={"results": {"user_question": {"question_id": "q1", "question": "확인?"}}},
        question_id="q1",
        resume_next=None,
    )

    resumed = await service.resume(principal=p, task_id="wf_wait", question_id="q1", answer="A", registry=ToolRegistry())

    assert resumed.state == "NEEDS_ATTENTION"
    assert "waiting step has no next step" in (resumed.reason or "")


@pytest.mark.asyncio
async def test_resume_respects_sync_wait_budget(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"action": "answer", "reason": "answered"},
        {"answer": "완료"},
        {"decision": "pass", "reason": "ok"},
    ])
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: provider, bus=MessageBus(), sync_wait_seconds=0)
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_wait",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_wait", state="WAITING_USER", question="확인?", question_id="q1", deliver="question"),
        context={"user_text": "질문"},
        data={"results": {"user_question": {"question_id": "q1", "question": "확인?"}}},
        question_id="q1",
        resume_next=None,
    )

    resumed = await service.resume(principal=p, task_id="wf_wait", question_id="q1", answer="A", registry=ToolRegistry())

    assert resumed.state == "RUNNING"
    await service.stop()


@pytest.mark.asyncio
async def test_workflow_runtime_control_resume_passes_real_message_content(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"action": "answer", "reason": "answered"},
        {"answer": "완료"},
        {"decision": "pass", "reason": "ok"},
    ])
    bus = MessageBus()
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: provider, bus=bus)
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_wait",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_wait", state="WAITING_USER", question="확인?", question_id="q1", deliver="question"),
        context={"user_text": "질문"},
        data={"results": {"user_question": {"question_id": "q1", "question": "확인?"}}},
        question_id="q1",
        resume_next=None,
    )
    state = MagicMock()
    state.workflow_service = service
    state.tools_config = ToolsConfig(workflow={"enabled": True})
    state.workspace = tmp_path
    state.bus = bus
    state.sessions = SessionManager(tmp_path)
    state.provider = provider
    msg = InboundMessage(
        channel="telegram",
        sender_id="1",
        chat_id="1",
        content="실제 명시 답장",
        metadata={"workflow_task_id": "wf_wait", "workflow_question_id": "q1", "message_id": "m-runtime"},
    )

    handled = await handle_workflow_runtime_control(state, msg, ToolRegistry())

    assert handled is True
    row = service.store.get_task("wf_wait")
    assert row is not None
    assert row["data"]["results"]["user_answer"] == {
        "question_id": "q1",
        "answer": "실제 명시 답장",
        "source": "runtime_control_message",
    }
    outbound = await bus.consume_outbound()
    assert outbound.content == "완료"


@pytest.mark.asyncio
async def test_workflow_tool_resume_requires_session_user_text(tmp_path: Path) -> None:
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: ScriptedProvider([]), bus=MessageBus())
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_wait",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_wait", state="WAITING_USER", question="확인?", question_id="q1", deliver="question"),
        context={"user_text": "질문"},
        data={"results": {"user_question": {"question_id": "q1", "question": "확인?"}}},
        question_id="q1",
        resume_next=None,
    )
    ctx = ToolContext(
        config=ToolsConfig(workflow={"enabled": True}),
        workspace=str(tmp_path),
        sessions=SessionManager(tmp_path),
        workflow_service=service,
        workflow_registry=ToolRegistry(),
    )
    token = bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key="telegram:1", message_id="missing"))
    try:
        tool = WorkflowTool.create(ctx)
        result = json.loads(await tool.execute(action="resume", task_id="wf_wait", question_id="q1", input="LLM 답변"))
    finally:
        reset_request_context(token)

    assert result["state"] == "NEEDS_ATTENTION"
    assert "current user message from session history" in result["reason"]
    row = service.store.get_task("wf_wait")
    assert row is not None
    assert "user_answer" not in row["data"].get("results", {})


@pytest.mark.asyncio
async def test_resume_returns_needs_attention_when_required_tool_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    definition = load_definition().model_copy(deep=True)
    definition.referenced_tools = ["web_search"]
    choose = next(step for step in definition.steps if step.id == "choose_action")
    choose.config["branches"]["search"]["required"] = True
    monkeypatch.setattr(WorkflowService, "_load_definition", staticmethod(lambda _definition_id: definition))
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: ScriptedProvider([]), bus=MessageBus())
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_wait",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_wait", state="WAITING_USER", question="확인?", question_id="q1", deliver="question"),
        context={"user_text": "질문"},
        data={"results": {"user_question": {"question_id": "q1", "resume_next": "judge_sufficiency"}}},
        question_id="q1",
        resume_next="judge_sufficiency",
    )

    resumed = await service.resume(principal=p, task_id="wf_wait", question_id="q1", answer="A", registry=ToolRegistry())

    assert resumed.state == "NEEDS_ATTENTION"
    assert "required tools are unavailable: web_search" in (resumed.reason or "")


@pytest.mark.asyncio
async def test_workflow_tool_builds_context_from_session_not_llm_input(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"ack": True},
        {"action": "answer", "reason": "enough"},
        {"answer": "세션 원문 기준 답변"},
        {"decision": "pass", "reason": "ok"},
    ])
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("telegram:1")
    session.metadata["goal_state"] = {"status": "active", "objective": "세션 목표"}
    session.metadata["conversation_focus"] = {"current_focus": "세션 초점"}
    session.add_message("assistant", "이전 답변")
    session.add_message("user", "진짜 사용자 원문", message_id="m1")
    sessions.save(session)
    service = WorkflowService(workspace=tmp_path, provider_loader=lambda: provider, bus=MessageBus())
    ctx = ToolContext(
        config=ToolsConfig(workflow={"enabled": True}),
        workspace=str(tmp_path),
        sessions=sessions,
        workflow_service=service,
        workflow_registry=ToolRegistry(),
    )
    token = bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key="telegram:1", message_id="m1"))
    try:
        tool = WorkflowTool.create(ctx)
        result = json.loads(await tool.execute(action="run", input="LLM이 지어낸 다른 요청"))
    finally:
        reset_request_context(token)

    assert result["state"] == "COMPLETED"
    first_payload = provider.payloads[0]
    system_context = first_payload["inputs"]["system_context"]
    assert system_context["user_text"] == "진짜 사용자 원문"
    assert system_context["recent_history"][-1]["content"] == "진짜 사용자 원문"
    assert system_context["goal_lines"]
    assert system_context["focus_lines"]
    row = service.store.get_task(result["task_id"])
    assert row is not None
    assert row["context"]["user_text"] == "진짜 사용자 원문"
    assert row["context"]["goal"] == "LLM이 지어낸 다른 요청"
    assert row["context"]["user_text_source"] == "message_id"


@pytest.mark.asyncio
async def test_agent_loop_process_direct_workflow_matches_current_message_id(tmp_path: Path) -> None:
    provider = AgentWorkflowProvider([
        {"ack": True},
        {"action": "answer", "reason": "enough"},
        {"answer": "프로세스 원문 기준 답변"},
        {"decision": "pass", "reason": "ok"},
    ])
    cfg = Config(
        agents={"defaults": {"workspace": str(tmp_path), "max_tool_iterations": 3}},
        tools={"workflow": {"enabled": True}},
    )
    loop = AgentLoop.from_config(cfg, provider=provider)

    outbound = await loop.process_direct(
        "실제 process_direct 사용자 원문",
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
    )
    await loop.close_mcp()

    assert outbound is not None
    assert provider.workflow_payloads
    system_context = provider.workflow_payloads[0]["inputs"]["system_context"]
    assert system_context["user_text"] == "실제 process_direct 사용자 원문"
    session = loop.sessions.get_or_create("cli:direct")
    current_message_id = next(
        msg["message_id"] for msg in session.messages
        if msg.get("role") == "user" and msg.get("content") == "실제 process_direct 사용자 원문"
    )
    row = next(iter(loop.workflow_service.store.list_tasks(principal=WorkflowPrincipal(
        workspace=str(tmp_path.resolve()),
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
        message_id=current_message_id,
    ))))
    assert row["context"]["user_text"] == "실제 process_direct 사용자 원문"
    assert row["context"]["goal"] == "LLM이 바꿔 쓴 사용자 요청"
    assert row["context"]["user_text_source"] == "message_id"


@pytest.mark.asyncio
async def test_gateway_process_message_workflow_matches_current_message_id(tmp_path: Path) -> None:
    provider = AgentWorkflowProvider([
        {"ack": True},
        {"action": "answer", "reason": "enough"},
        {"answer": "게이트웨이 원문 기준 답변"},
        {"decision": "pass", "reason": "ok"},
    ])
    cfg = Config(
        agents={"defaults": {"workspace": str(tmp_path), "max_tool_iterations": 3}},
        tools={"workflow": {"enabled": True}},
    )
    loop = AgentLoop.from_config(cfg, provider=provider)
    msg = InboundMessage(
        channel="telegram",
        sender_id="1",
        chat_id="1",
        content="실제 게이트웨이 사용자 원문",
        metadata={"message_id": "gateway-message-1"},
    )

    outbound = await loop._process_message(msg, session_key="telegram:1")
    await loop.close_mcp()

    assert outbound is not None
    assert provider.workflow_payloads
    system_context = provider.workflow_payloads[0]["inputs"]["system_context"]
    assert system_context["user_text"] == "실제 게이트웨이 사용자 원문"
    assert system_context["user_text_source"] == "message_id"
    rows = loop.workflow_service.store.list_tasks(principal=WorkflowPrincipal(
        workspace=str(tmp_path.resolve()),
        session_key="telegram:1",
        channel="telegram",
        chat_id="1",
        message_id="gateway-message-1",
    ))
    assert rows
    assert rows[0]["context"]["user_text"] == "실제 게이트웨이 사용자 원문"
    assert rows[0]["context"]["user_text_source"] == "message_id"


@pytest.mark.asyncio
async def test_workflow_tool_uses_service_for_list_status_cancel(tmp_path: Path) -> None:
    service = WorkflowService(workspace=tmp_path, provider_loader=None, bus=MessageBus())
    cfg = ToolsConfig(workflow={"enabled": True})
    ctx = ToolContext(
        config=cfg,
        workspace=str(tmp_path),
        workflow_service=service,
        workflow_registry=ToolRegistry(),
    )
    token = bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key="telegram:1"))
    try:
        tool = WorkflowTool.create(ctx)
        listed = json.loads(await tool.execute(action="list"))
        assert listed[0]["id"] == "situation_judgment.v1"
        run = json.loads(await tool.execute(action="run", input="hello"))
        assert run["state"] == "NEEDS_ATTENTION"
        assert "current user message from session history" in run["reason"]
    finally:
        reset_request_context(token)


def test_agent_loop_omits_workflow_delivery_hook_when_workflow_disabled(tmp_path: Path) -> None:
    cfg = Config(agents={"defaults": {"workspace": str(tmp_path)}}, tools={"workflow": {"enabled": False}})
    loop = AgentLoop.from_config(cfg, provider=ScriptedProvider([]))

    assert loop.workflow_enabled is False
    assert loop.workflow_service is None
    assert not any(isinstance(hook, WorkflowDeliveryHook) for hook in loop._extra_hooks)


def test_agent_loop_adds_workflow_delivery_hook_when_workflow_enabled(tmp_path: Path) -> None:
    cfg = Config(agents={"defaults": {"workspace": str(tmp_path)}}, tools={"workflow": {"enabled": True}})
    loop = AgentLoop.from_config(cfg, provider=ScriptedProvider([]))

    assert loop.workflow_enabled is True
    assert loop.workflow_service is not None
    assert any(isinstance(hook, WorkflowDeliveryHook) for hook in loop._extra_hooks)


def test_runtime_lines_expose_only_same_session_waiting_tasks(tmp_path: Path) -> None:
    service = WorkflowService(workspace=tmp_path, provider_loader=None, bus=MessageBus())
    p = principal(tmp_path)
    service.store.upsert_task(
        task_id="wf_wait",
        principal=p,
        definition_id="situation_judgment.v1",
        envelope=WorkflowEnvelope(task_id="wf_wait", state="WAITING_USER", question="확인?", question_id="q1", deliver="question"),
        context={},
        question_id="q1",
    )
    state = MagicMock()
    state.workflow_enabled = True
    state.workflow_service = service
    state._mcp_servers = {}
    state._mcp_stacks = {}
    msg = InboundMessage(channel="telegram", sender_id="1", chat_id="1", content="x")
    lines = runtime_lines(state, msg, tmp_path)
    assert any("workflow waiting: task_id=wf_wait" in line for line in lines)


def test_workflow_delivery_hook_replaces_final_content() -> None:
    from nanobot.agent.hook import AgentHookContext
    from nanobot.workflow.delivery import set_workflow_turn_delivery

    clear_workflow_turn_delivery()
    set_workflow_turn_delivery("wf1", "검증된 답변")
    ctx = AgentHookContext(iteration=1, messages=[])
    assert WorkflowDeliveryHook().finalize_content(ctx, "바깥 답변") == "검증된 답변"
    assert ctx.tool_events and ctx.tool_events[0]["event"] == "final_content_replaced"
    clear_workflow_turn_delivery()

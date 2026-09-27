from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.context import runtime_lines
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext, ToolContext, bind_request_context, reset_request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.workflow import WorkflowTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config, ToolsConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.session.manager import SessionManager
from nanobot.workflow.delivery import WorkflowDeliveryHook, clear_workflow_turn_delivery
from nanobot.workflow.schema import WorkflowDefinition, WorkflowEnvelope, WorkflowPrincipal
from nanobot.workflow.service import WorkflowService


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
        assert "active provider" in run["reason"]
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

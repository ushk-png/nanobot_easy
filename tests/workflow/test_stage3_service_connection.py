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
from nanobot.workflow.delivery import WorkflowDeliveryHook, clear_workflow_turn_delivery
from nanobot.workflow.schema import WorkflowPrincipal
from nanobot.workflow.service import WorkflowService


class ScriptedProvider(LLMProvider):
    def __init__(self, script: list[dict[str, Any]]):
        super().__init__()
        self.script = list(script)

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
        return LLMResponse(content=json.dumps(self.script.pop(0), ensure_ascii=False))

    def get_default_model(self) -> str:
        return "scripted"


def principal(tmp_path: Path) -> WorkflowPrincipal:
    return WorkflowPrincipal(
        workspace=str(tmp_path.resolve()),
        session_key="telegram:1",
        channel="telegram",
        chat_id="1",
    )


@pytest.mark.asyncio
async def test_workflow_service_persists_waiting_and_resume_access(tmp_path: Path) -> None:
    service = WorkflowService(
        workspace=tmp_path,
        provider_loader=lambda: ScriptedProvider([
            {"ack": True},
            {"action": "ask_user", "reason": "need preference", "question": "선호?"},
        ]),
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
    )
    assert resumed.state == "COMPLETED"
    assert resumed.reason == "A"


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
    from nanobot.workflow.schema import WorkflowEnvelope
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

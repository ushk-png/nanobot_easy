from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.workflow.composition import compose_workflow_definition
from nanobot.workflow.evaluation import WorkflowEvaluator
from nanobot.workflow.executor import WorkflowExecutionResult
from nanobot.workflow.schema import WorkflowEnvelope, WorkflowPrincipal, WorkflowDefinition
from nanobot.workflow.service import WorkflowService


class ScriptedProvider(LLMProvider):
    def __init__(self, script: list[dict[str, Any]]):
        super().__init__()
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append({
            "system": str(messages[0]["content"]),
            "payload": json.loads(messages[-1]["content"]),
        })
        if not self.script:
            raise AssertionError("script exhausted")
        return LLMResponse(content=json.dumps(self.script.pop(0), ensure_ascii=False))

    def get_default_model(self) -> str:
        return "scripted"


@tool_parameters(tool_parameters_schema(query=StringSchema("query"), required=["query"], additional_properties=None))
class EchoMemoryTool(Tool):
    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return "memory"

    async def execute(self, **kwargs: Any) -> str:
        return "memory:" + str(kwargs.get("query"))


def load_definition() -> WorkflowDefinition:
    path = Path("nanobot/workflow/definitions/situation_judgment.v1.json")
    return WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))


def registry(*tools: Tool) -> ToolRegistry:
    r = ToolRegistry()
    for tool in tools:
        r.register(tool)
    return r


def principal(tmp_path: Path) -> WorkflowPrincipal:
    return WorkflowPrincipal(
        workspace=str(tmp_path.resolve()),
        session_key="telegram:1",
        channel="telegram",
        chat_id="1",
    )


def test_composer_removes_unavailable_optional_branches_and_steps() -> None:
    composed, report = compose_workflow_definition(
        load_definition(),
        available_tools={"search_memory"},
        request_text="질문",
    )
    step_ids = {step.id for step in composed.steps}
    branch = next(step for step in composed.steps if step.id == "choose_action")
    branches = branch.config["branches"]

    assert "memory" in branches
    assert "search" not in branches
    assert "web_search" not in step_ids
    assert "search_memory" in step_ids
    assert report.selected_tools == ["search_memory"]
    assert report.removed_tools == ["web_search"]
    assert "search" in report.removed_branches


def test_evaluator_converts_empty_completed_answer_to_needs_attention() -> None:
    result = WorkflowExecutionResult(
        envelope=WorkflowEnvelope(task_id="wf1", state="COMPLETED", deliver="final", reason=""),
        data={},
        trace=[],
    )
    evaluated, report = WorkflowEvaluator().evaluate(result)
    assert not report.passed
    assert evaluated.envelope.state == "NEEDS_ATTENTION"
    assert "no final answer" in (evaluated.envelope.reason or "")


@pytest.mark.asyncio
async def test_service_stores_composition_and_evaluation_reports(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        {"ack": True},
        {"action": "memory", "reason": "need memory"},
        {"action": "answer", "reason": "memory observed"},
        {"answer": "기억 기반 답변"},
        {"decision": "pass", "reason": "ok"},
    ])
    service = WorkflowService(
        workspace=tmp_path,
        provider_loader=lambda: provider,
        bus=MessageBus(),
        sync_wait_seconds=60,
    )
    env = await service.run(
        principal=principal(tmp_path),
        user_text="이전 대화 기준으로 답해줘",
        registry=registry(EchoMemoryTool()),
    )
    assert env.state == "COMPLETED"
    assert env.reason == "기억 기반 답변"
    row = service.store.get_task(env.task_id or "")
    assert row is not None
    assert row["context"]["workflow_composition"]["selected_tools"] == ["search_memory"]
    assert row["data"]["evaluation"]["passed"] is True
    assert "web_search" in row["context"]["workflow_composition"]["removed_tools"]
    # Prompts seen by the provider include composition annotations.
    assert any("Dynamic workflow composition" in call["system"] for call in provider.calls)

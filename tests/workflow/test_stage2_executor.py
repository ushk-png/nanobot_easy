from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.agent.tools.workflow import WorkflowTool
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.workflow.executor import WorkflowExecutor
from nanobot.workflow.llm_adapter import WorkflowLLMAdapter
from nanobot.workflow.schema import WorkflowDefinition
from nanobot.workflow.validator import validate_definition


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
        self.calls.append(json.loads(messages[-1]["content"]))
        if not self.script:
            raise AssertionError("script exhausted")
        return LLMResponse(content=json.dumps(self.script.pop(0), ensure_ascii=False))

    def get_default_model(self) -> str:
        return "scripted-test-model"


@tool_parameters(tool_parameters_schema(query=StringSchema("query"), required=["query"], additional_properties=None))
class EchoMemoryTool(Tool):
    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return "test memory tool"

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return "memory:" + str(kwargs.get("query"))


@tool_parameters(tool_parameters_schema(query=StringSchema("query"), required=["query"], additional_properties=None))
class EchoWebSearchTool(Tool):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "test search tool"

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return "search:" + str(kwargs.get("query"))


def load_definition() -> WorkflowDefinition:
    path = Path("nanobot/workflow/definitions/situation_judgment.v1.json")
    return WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))


def registry(*tools: Tool) -> ToolRegistry:
    r = ToolRegistry()
    for tool in tools:
        r.register(tool)
    return r


async def run_script(script: list[dict[str, Any]], tools: ToolRegistry | None = None):
    provider = ScriptedProvider(script)
    executor = WorkflowExecutor(
        definition=load_definition(),
        tools=tools or registry(),
        llm=WorkflowLLMAdapter(provider),
        context_snapshot={"user_text": "질문", "available_tools": []},
    )
    result = await executor.run()
    return result, provider


def test_default_definition_validates_with_optional_missing_tools() -> None:
    definition = load_definition()
    assert validate_definition(definition, available_tools=set()).ok


@pytest.mark.asyncio
async def test_answer_branch_completes_with_review_pass() -> None:
    result, _provider = await run_script([
        {"ack": True},
        {"action": "answer", "reason": "enough"},
        {"answer": "최종 답변"},
        {"decision": "pass", "reason": "ok"},
    ])
    assert result.envelope.state == "COMPLETED"
    assert result.envelope.deliver == "final"
    assert result.envelope.reason == "최종 답변"


@pytest.mark.asyncio
async def test_memory_branch_runs_tool_then_completes() -> None:
    result, provider = await run_script([
        {"ack": True},
        {"action": "memory", "reason": "need memory"},
        {"action": "answer", "reason": "memory observed"},
        {"answer": "기억 기반 답변"},
        {"decision": "pass", "reason": "ok"},
    ], registry(EchoMemoryTool()))
    assert result.envelope.state == "COMPLETED"
    assert result.data["results"]["memory_result"].startswith("memory:")
    assert len(provider.calls) == 5


@pytest.mark.asyncio
async def test_search_branch_runs_tool_then_completes() -> None:
    result, _provider = await run_script([
        {"ack": True},
        {"action": "search", "reason": "need current info"},
        {"action": "answer", "reason": "search observed"},
        {"answer": "검색 기반 답변"},
        {"decision": "pass", "reason": "ok"},
    ], registry(EchoWebSearchTool()))
    assert result.envelope.state == "COMPLETED"
    assert result.data["results"]["search_result"].startswith("search:")


@pytest.mark.asyncio
async def test_ask_user_branch_returns_waiting_user() -> None:
    result, _provider = await run_script([
        {"ack": True},
        {"action": "ask_user", "reason": "need preference", "question": "선호가 뭔가요?"},
    ])
    assert result.envelope.state == "WAITING_USER"
    assert result.envelope.deliver == "question"
    assert result.envelope.question == "선호가 뭔가요?"
    assert result.envelope.question_id


@pytest.mark.asyncio
async def test_optional_unavailable_search_branch_is_pruned() -> None:
    result, _provider = await run_script([
        {"ack": True},
        {"action": "search", "reason": "need search"},
    ], registry())
    assert result.envelope.state == "NEEDS_ATTENTION"


@pytest.mark.asyncio
async def test_workflow_tool_without_service_returns_needs_attention(tmp_path: Path) -> None:
    tool = WorkflowTool.create(ToolContext(config=ToolsConfig(workflow={"enabled": True}), workspace=str(tmp_path)))
    missing = json.loads(await tool.execute(action="run", input="hello"))
    assert missing["state"] == "NEEDS_ATTENTION"
    assert "workflow service is not available" in missing["reason"]

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.tools.context import ToolContext, bind_request_context, reset_request_context, RequestContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig
from nanobot.workflow.access import build_principal, ensure_task_access, WorkflowAccessDenied
from nanobot.workflow.conditions import evaluate_condition
from nanobot.workflow.context_builder import build_workflow_context_snapshot
from nanobot.workflow.schema import WorkflowDefinition, WorkflowTaskRecord, WorkflowDefinitionRef, WorkflowEnvelope
from nanobot.workflow.validator import validate_definition


def test_workflow_tool_disabled_by_default_not_loaded(tmp_path: Path) -> None:
    cfg = ToolsConfig()
    assert cfg.workflow.enabled is False
    registry = ToolRegistry()
    ctx = ToolContext(config=cfg, workspace=str(tmp_path))
    ToolLoader().load(ctx, registry, scope="core")
    assert not registry.has("workflow")


@pytest.mark.asyncio
async def test_workflow_tool_enabled_returns_stage1_contract_envelope(tmp_path: Path) -> None:
    cfg = ToolsConfig(workflow={"enabled": True})
    registry = ToolRegistry()
    ctx = ToolContext(config=cfg, workspace=str(tmp_path))
    ToolLoader().load(ctx, registry, scope="core")
    tool = registry.get("workflow")
    assert tool is not None
    raw = await tool.execute(action="status", task_id="wf_test")
    payload = json.loads(raw)
    assert payload["state"] == "NEEDS_ATTENTION"
    assert payload["delivery_state"] == "pending"
    assert payload["deliver"] == "none"
    assert "workflow service is not available" in payload["reason"]


def test_principal_built_from_runtime_context_not_llm_args(tmp_path: Path) -> None:
    token = bind_request_context(RequestContext(
        channel="telegram",
        chat_id="8580974491",
        message_id="42",
        session_key="telegram:8580974491",
        metadata={"ignored": "ok"},
    ))
    try:
        principal = build_principal(ToolContext(config=ToolsConfig(), workspace=str(tmp_path)))
    finally:
        reset_request_context(token)
    assert principal.workspace == str(tmp_path.resolve())
    assert principal.session_key == "telegram:8580974491"
    assert principal.channel == "telegram"
    assert principal.chat_id == "8580974491"
    assert principal.message_id == "42"


def test_task_access_requires_same_workspace_and_session(tmp_path: Path) -> None:
    token = bind_request_context(RequestContext(
        channel="telegram",
        chat_id="1",
        session_key="telegram:1",
    ))
    try:
        principal = build_principal(ToolContext(config=ToolsConfig(), workspace=str(tmp_path)))
    finally:
        reset_request_context(token)
    task = WorkflowTaskRecord(
        task_id="wf_1",
        principal=principal,
        definition=WorkflowDefinitionRef(id="situation_judgment.v1", version="1.0.0"),
        request="hello",
    )
    ensure_task_access(principal, task)
    denied = principal.model_copy(update={"session_key": "telegram:2"})
    with pytest.raises(WorkflowAccessDenied):
        ensure_task_access(denied, task)


def test_definition_validation_rejects_recursive_workflow_tool() -> None:
    definition = WorkflowDefinition.model_validate({
        "id": "bad.v1",
        "version": "1.0.0",
        "start": "call_workflow",
        "steps": [
            {"id": "call_workflow", "type": "tool", "next": "done", "config": {"tool": "workflow"}},
            {"id": "done", "type": "end"},
        ],
    })
    result = validate_definition(definition, available_tools={"workflow"})
    assert not result.ok
    assert any("may not call tool" in error for error in result.errors)


def test_condition_evaluator_has_no_eval_exec_path() -> None:
    data = {"judgment": {"sufficient": True, "score": 3}}
    assert evaluate_condition({"op": "eq", "path": "judgment.sufficient", "value": True}, data)
    assert evaluate_condition({"op": "gte", "path": "judgment.score", "value": 3}, data)
    assert evaluate_condition({"op": "exists", "path": "judgment.score"}, data)


def test_context_snapshot_contains_required_contract_fields(tmp_path: Path) -> None:
    principal = build_principal(ToolContext(config=ToolsConfig(), workspace=str(tmp_path)))
    registry = ToolRegistry()
    snapshot = build_workflow_context_snapshot(
        user_text="사용자 원문",
        principal=principal,
        session_metadata={"goal_state": {"active": False}},
        recent_history=[{"role": "user", "content": "이전"}],
        tools=registry,
        remaining_iterations=5,
        same_session_workflows=[{"task_id": "wf_existing", "state": "WAITING_USER"}],
    )
    assert snapshot["user_text"] == "사용자 원문"
    assert snapshot["principal"]["workspace"] == str(tmp_path.resolve())
    assert snapshot["recent_history"]
    assert snapshot["available_tools"] == []
    assert snapshot["remaining_iterations"] == 5
    assert snapshot["same_session_workflows"][0]["state"] == "WAITING_USER"


def test_default_definition_contract_loads() -> None:
    path = Path("nanobot/workflow/definitions/situation_judgment.v1.json")
    definition = WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))
    assert definition.start == "receive_context"
    assert validate_definition(definition).ok
    envelope = WorkflowEnvelope.disabled()
    assert envelope.state == "NEEDS_ATTENTION"
    assert envelope.delivery_state == "pending"

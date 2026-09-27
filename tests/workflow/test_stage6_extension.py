from __future__ import annotations

from typing import Any

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.workflow.extension import (
    CandidateEvaluation,
    FunctionCandidate,
    WorkflowCandidateRegistry,
    promote_candidate_to_registry,
    rollback_registry,
    validate_candidate,
    validate_candidate_inputs,
    validate_candidate_output,
)


class EchoTool(Tool):
    def __init__(self, name: str = "candidate_echo") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "echo test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> dict[str, str]:
        return {"text": str(kwargs["text"])}


def verified_candidate(name: str = "candidate_echo") -> FunctionCandidate:
    return FunctionCandidate(
        name=name,
        version="1.0.0",
        purpose="Echo text for workflow extension testing",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        preconditions=["text is user-provided test data"],
        error_types=["missing_text"],
        permissions=["read_only"],
        external_side_effects=False,
        retryable=True,
        evaluation=CandidateEvaluation(
            normal_cases=1,
            error_cases=1,
            boundary_cases=1,
            integration_cases=1,
            passed_cases=4,
            failed_cases=0,
        ),
        status="verified",
        implementation=EchoTool(name),
    )


def test_verified_candidate_requires_complete_evidence_and_implementation() -> None:
    bad = FunctionCandidate(
        name="candidate_echo",
        version="1.0.0",
        purpose="Echo text",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        status="verified",
        evaluation=CandidateEvaluation(normal_cases=1, passed_cases=1, failed_cases=0),
        implementation=None,
    )

    result = validate_candidate(bad)

    assert not result.ok
    assert any("implementation" in error for error in result.errors)
    assert any("normal, error, boundary, and integration" in error for error in result.errors)


def test_reserved_or_mismatched_candidate_is_rejected() -> None:
    reserved = verified_candidate("workflow")
    mismatch = FunctionCandidate(
        name="candidate_echo",
        version="1.0.0",
        purpose="Echo text",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        status="draft",
        implementation=EchoTool("other_name"),
    )

    assert not validate_candidate(reserved).ok
    mismatch_result = validate_candidate(mismatch)
    assert not mismatch_result.ok
    assert any("must match" in error for error in mismatch_result.errors)


def test_candidate_input_and_output_schema_validation() -> None:
    candidate = verified_candidate()

    assert validate_candidate_inputs(candidate, {"text": "ok"}) == []
    assert validate_candidate_inputs(candidate, {"text": 1})
    assert validate_candidate_output(candidate, {"text": "ok"}) == []
    assert validate_candidate_output(candidate, {"text": 1})


def test_candidate_registry_lists_only_verified_candidates() -> None:
    registry = WorkflowCandidateRegistry()
    draft = FunctionCandidate(
        name="draft_tool",
        version="0.1.0",
        purpose="Draft only",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        status="draft",
    )
    verified = verified_candidate()

    assert registry.add(draft).ok
    assert registry.add(verified).ok

    assert [item["name"] for item in registry.list(status="verified")] == ["candidate_echo"]
    assert [candidate.name for candidate in registry.verified()] == ["candidate_echo"]


@pytest.mark.asyncio
async def test_promote_candidate_uses_registry_snapshot_and_rollback() -> None:
    original = ToolRegistry()
    original.register(EchoTool("existing_echo"))
    candidate = verified_candidate()

    promoted, record = promote_candidate_to_registry(original, candidate)

    assert record.promoted is True
    assert promoted.has("candidate_echo")
    assert not original.has("candidate_echo")
    tool, params, error = promoted.prepare_call("candidate_echo", {"text": "hello"})
    assert error is None
    assert tool is not None
    assert await tool.execute(**params) == {"text": "hello"}

    rolled_back = rollback_registry(promoted, original)
    assert rolled_back.has("existing_echo")
    assert not rolled_back.has("candidate_echo")


def test_unverified_candidate_is_not_promoted() -> None:
    original = ToolRegistry()
    candidate = FunctionCandidate(
        name="draft_tool",
        version="0.1.0",
        purpose="Draft only",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        status="draft",
        implementation=EchoTool("draft_tool"),
    )

    promoted, record = promote_candidate_to_registry(original, candidate)

    assert record.promoted is False
    assert "verified candidate requires" in record.reason or "status" in record.reason
    assert not promoted.has("draft_tool")

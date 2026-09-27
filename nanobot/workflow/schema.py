"""Workflow contract DTOs for the staged workflow engine.

Stage 1 intentionally defines the durable contracts only. Execution semantics
live in later modules/stages.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from nanobot.config_base import Base

WorkflowState = Literal[
    "CREATED",
    "RUNNING",
    "WAITING_USER",
    "COMPLETED",
    "NEEDS_ATTENTION",
    "FAILED",
    "CANCELLED",
]
DeliveryState = Literal["pending", "handed_to_channel", "delivered", "failed"]
DeliverMode = Literal["none", "final", "question", "background"]
StepType = Literal["llm", "tool", "branch", "wait_user", "end"]


class WorkflowAction(str, Enum):
    RUN = "run"
    RESUME = "resume"
    STATUS = "status"
    CANCEL = "cancel"
    LIST = "list"


class WorkflowPrincipal(Base):
    """Caller identity used for workflow access checks.

    The principal is built by trusted runtime code from RequestContext and
    ToolContext. LLM-provided arguments must not supply these fields.
    """

    workspace: str
    session_key: str
    channel: str
    chat_id: str
    message_id: str | None = None
    subagent_depth: int = Field(default=0, ge=0)


class WorkflowBudget(Base):
    max_steps: int = Field(default=40, ge=1)
    max_llm_calls: int = Field(default=8, ge=0)
    max_tool_calls: int = Field(default=12, ge=0)
    max_retries: int = Field(default=3, ge=0)
    wall_time_seconds: int = Field(default=600, ge=1)


class WorkflowDefinitionRef(Base):
    id: str
    version: str
    content_hash: str | None = None


class WorkflowStep(Base):
    id: str
    type: StepType
    next: str | None = None
    on_error: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinition(Base):
    id: str
    version: str
    title: str = ""
    description: str = ""
    start: str
    steps: list[WorkflowStep]
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)
    referenced_skills: list[str] = Field(default_factory=list)
    referenced_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_start_and_unique_steps(self) -> "WorkflowDefinition":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("workflow step ids must be unique")
        if self.start not in set(ids):
            raise ValueError(f"workflow start step {self.start!r} does not exist")
        return self


class WorkflowTaskRecord(Base):
    task_id: str
    state: WorkflowState = "CREATED"
    delivery_state: DeliveryState = "pending"
    principal: WorkflowPrincipal
    definition: WorkflowDefinitionRef
    request: str
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    current_step: str | None = None
    budget_remaining: WorkflowBudget = Field(default_factory=WorkflowBudget)


class WorkflowQuestion(Base):
    question_id: str
    text: str
    task_id: str


class WorkflowEnvelope(Base):
    """Public workflow tool response contract.

    Matches the staged directive's shape:
    {task_id, state, delivery_state, deliver, question, question_id, reason, next_hint}
    """

    task_id: str | None = None
    state: WorkflowState | None = None
    delivery_state: DeliveryState = "pending"
    deliver: DeliverMode = "none"
    question: str | None = None
    question_id: str | None = None
    reason: str | None = None
    next_hint: str | None = None

    @classmethod
    def disabled(cls) -> "WorkflowEnvelope":
        return cls(
            state="NEEDS_ATTENTION",
            reason="workflow tool is disabled by config.tools.workflow.enabled=false",
            next_hint="Enable config.tools.workflow.enabled only after staged implementation reaches the required connection stage.",
        )


class WorkflowRunRequest(Base):
    workflow_id: str = "situation_judgment.v1"
    input: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class WorkflowResumeRequest(Base):
    task_id: str
    question_id: str | None = None
    response: str


class WorkflowStatusRequest(Base):
    task_id: str


class WorkflowCancelRequest(Base):
    task_id: str
    reason: str | None = None

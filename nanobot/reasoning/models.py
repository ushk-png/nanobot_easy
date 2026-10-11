"""State objects for code-controlled staged reasoning.

The code owns step transitions. An LLM may propose judgments, but a model
string such as "verified" never becomes a system verification state.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import Field

from nanobot.config_base import Base

StepId = str
ProcedureKind = Literal[
    "method-evaluation",
    "cause-analysis",
    "problem-solving",
    "document-review",
    "rule-judgment",
]
FactStatus = Literal["confirmed", "estimated", "unknown"]
CriterionKind = Literal["required", "preferred"]
JudgmentResult = Literal["met", "unmet", "unknown"]
ConclusionStatus = Literal["suitable", "conditionally_suitable", "unsuitable", "deferred"]
StepState = Literal[
    "pending",
    "running",
    "passed",
    "conditionally_passed",
    "waiting_info",
    "needs_revision",
    "invalidated",
    "stopped",
    "failed_empty_answer",
]
CaseStatus = Literal[
    "running",
    "completed",
    "waiting_info",
    "deferred",
    "stopped",
    "failed_empty_answer",
]


class StepBudget(Base):
    max_input_tokens: int = Field(default=4000, ge=1)
    max_output_tokens: int = Field(default=1200, ge=1)
    max_calls: int = Field(default=2, ge=1)


class ProcedureStep(Base):
    id: StepId
    role: str = "work"
    question: str
    inputs: list[str]
    outputs: list[str]
    checks: list[str]
    failure_returns_to: StepId | None = None
    budget: StepBudget = Field(default_factory=StepBudget)
    version: str = "3.1"


class Procedure(Base):
    id: str
    version: str
    revision: int = 1
    title: str
    purpose: str = ""
    requirements: list[str] = Field(default_factory=list)
    steps: list[ProcedureStep]
    dynamic: bool = False
    reusable: bool = False

    def step(self, step_id: str) -> ProcedureStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)


class Fact(Base):
    id: str
    proposition: str
    source: str
    status: FactStatus = "unknown"
    version: int = 1


class Criterion(Base):
    id: str
    statement: str
    kind: CriterionKind
    method: str
    linked_goal: str
    version: int = 1


class Judgment(Base):
    id: str
    criterion_id: str
    result: JudgmentResult
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    step_id: StepId = "S4"
    version: int = 1


class Conclusion(Base):
    id: str
    status: ConclusionStatus
    statement: str
    conditions: list[str] = Field(default_factory=list)
    judgment_ids: list[str] = Field(default_factory=list)
    version: int = 1


class CheckRecord(Base):
    id: str
    target_version: str
    kind: str
    result: str
    limit: str
    defects: list[str] = Field(default_factory=list)


class Usage(Base):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    duration_ms: int = 0
    reasoning_effort: str | None = None
    reasoning_disabled_guaranteed: bool = False


class ReasoningCase(Base):
    id: str
    request: str
    procedure_id: str
    procedure_version: str
    status: CaseStatus = "running"
    current_step: StepId = "S1"
    execution_revision: int = 1
    step_states: dict[str, StepState] = Field(default_factory=dict)
    goal: str = ""
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    judgments: list[Judgment] = Field(default_factory=list)
    conclusion: Conclusion | None = None
    checks: list[CheckRecord] = Field(default_factory=list)
    answer: str = ""
    usage: Usage = Field(default_factory=Usage)
    revision_count: int = 0
    stop_reason: str = ""
    history: list[dict[str, Any]] = Field(default_factory=list)
    step_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    call_records: list[dict[str, Any]] = Field(default_factory=list)


class Entailment(str, Enum):
    ENTAILED = "ENTAILED"
    REFUTED = "REFUTED"
    UNDETERMINED = "UNDETERMINED"
    INCONSISTENT = "INCONSISTENT"
    SOLVER_UNKNOWN = "SOLVER_UNKNOWN"

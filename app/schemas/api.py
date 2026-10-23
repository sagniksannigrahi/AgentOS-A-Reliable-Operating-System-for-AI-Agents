"""Public REST API request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.plan import AcceptanceCriterion

T = TypeVar("T")

WorkflowMode = Literal["supervised", "unsupervised", "single_agent"]


class TaskOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_plan_approval: bool | None = None
    max_subtask_attempts: int | None = Field(default=None, ge=1, le=6)
    max_graph_steps: int | None = Field(default=None, ge=5, le=200)
    max_total_tokens: int | None = Field(default=None, ge=1000)
    # --- evaluation-only options (rejected unless AGENTOS_EVALUATION_MODE=true) -------------
    workflow_mode: WorkflowMode = "supervised"
    loop_detection: bool = True
    recovery: bool = True
    llm_scenario: str | None = Field(default=None, max_length=64)
    faults: list[str] = Field(default_factory=list, max_length=20)
    seed: int | None = None

    def uses_evaluation_features(self) -> bool:
        return (
            self.workflow_mode != "supervised"
            or not self.loop_detection
            or not self.recovery
            or self.llm_scenario is not None
            or bool(self.faults)
            or self.seed is not None
        )


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20_000)
    workspace_seed: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,63}$")
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=20)
    options: TaskOptions = Field(default_factory=TaskOptions)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TaskRunOut(ORM):
    id: uuid.UUID
    attempt: int
    status: str
    reason: str
    worker_id: str | None
    recovered_from_run_id: uuid.UUID | None
    error: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskSummary(ORM):
    id: uuid.UUID
    title: str
    status: str
    workspace_seed: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    trace_id: str | None


class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    provider_reported_tokens: int = 0
    estimated_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    cost_usd_from_reported_tokens: Decimal = Decimal(0)
    cost_usd_from_estimated_tokens: Decimal = Decimal(0)
    calls: int = 0
    by_role: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TaskDetail(TaskSummary):
    description: str
    request: dict[str, Any]
    current_plan_version: int | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    paused_from: str | None
    plan: dict[str, Any] | None = None
    runs: list[TaskRunOut] = Field(default_factory=list)
    usage: UsageTotals = Field(default_factory=UsageTotals)
    latest_checkpoint: dict[str, Any] | None = None
    pending_approvals: int = 0
    counts: dict[str, int] = Field(default_factory=dict)


class EventOut(ORM):
    id: int
    seq: int
    run_id: uuid.UUID | None
    type: str
    status: str
    actor: str
    message: str
    data: dict[str, Any]
    agent_run_id: uuid.UUID | None
    tool_invocation_id: uuid.UUID | None
    trace_id: str | None
    span_id: str | None
    created_at: datetime


class EventPage(BaseModel):
    items: list[EventOut]
    next_after_seq: int | None


class AgentRunOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID
    role: str
    subtask_key: str | None
    attempt: int
    status: str
    replay_count: int
    input_summary: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    latency_ms: int | None
    trace_id: str | None
    span_id: str | None
    started_at: datetime
    finished_at: datetime | None
    tool_calls: int = 0
    tokens: int = 0


class ToolInvocationOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID
    agent_run_id: uuid.UUID | None
    agent_role: str
    tool_name: str
    status: str
    decision_reason: str | None
    mutating: bool
    args_redacted: dict[str, Any]
    output_preview: str | None
    output_truncated: bool
    attempts: int
    duration_ms: int | None
    error: dict[str, Any] | None
    trace_id: str | None
    created_at: datetime
    completed_at: datetime | None


class ArtifactOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID | None
    kind: str
    name: str
    content_type: str
    sha256: str
    size_bytes: int
    meta: dict[str, Any]
    created_at: datetime


class ArtifactContent(ArtifactOut):
    content: str


class TransitionOut(ORM):
    id: int
    run_id: uuid.UUID | None
    from_status: str
    to_status: str
    actor: str
    reason: str
    created_at: datetime


class CheckpointOut(ORM):
    id: int
    run_id: uuid.UUID
    thread_id: str
    checkpoint_id: str
    node: str | None
    step: int | None
    restored: bool
    created_at: datetime


class ApprovalOut(ORM):
    id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID | None
    kind: str
    status: str
    reason: str
    payload: dict[str, Any]
    requested_by_agent: str
    expires_at: datetime
    created_at: datetime
    decision: str | None = None
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    task_title: str | None = None


class ApprovalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="manual resume", max_length=500)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="cancelled by user", max_length=500)


class SpanOut(ORM):
    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    kind: str
    service: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    status_code: str
    attributes: dict[str, Any]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody

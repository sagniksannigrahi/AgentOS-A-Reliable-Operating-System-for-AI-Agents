"""SQLAlchemy ORM models. PostgreSQL is the source of truth for all durable state.

Consistency rules
-----------------
* ``tasks.status`` is the authoritative lifecycle state. It only changes through
  :mod:`app.persistence.repository.transition_task`, which also writes ``state_transitions`` and an
  event in the same transaction.
* LangGraph checkpoints (tables managed by ``langgraph-checkpoint-postgres``) are authoritative for
  *where the graph is*. ``checkpoint_refs`` only stores references to them, never a copy.
* Plans and subtasks are immutable once written; per-subtask progress lives in the graph state and
  is observable through ``agent_runs``. This avoids a second mutable copy of execution state.
* Append-only tables (events, transitions, tool invocations, usage) carry idempotency keys so that
  replaying a node after a crash does not duplicate records.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB, uuid.UUID: UUID(as_uuid=True)}


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[Any]] = mapped_column(default=list)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    request: Mapped[dict[str, Any]] = mapped_column(default=dict)
    workspace_seed: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    event_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    current_plan_version: Mapped[int | None] = mapped_column(Integer)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_from: Mapped[str | None] = mapped_column(String(32))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    traceparent: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    runs: Mapped[list[TaskRun]] = relationship(back_populates="task", order_by="TaskRun.attempt")


class StateTransition(TimestampMixin, Base):
    __tablename__ = "state_transitions"
    __table_args__ = (Index("ix_transitions_task", "task_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(32))


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("task_id", "version", name="uq_plans_task_version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column()
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    reason: Mapped[str] = mapped_column(Text, default="initial")

    subtasks: Mapped[list[Subtask]] = relationship(back_populates="plan", order_by="Subtask.order")


class Subtask(Base):
    __tablename__ = "subtasks"
    __table_args__ = (UniqueConstraint("plan_id", "key", name="uq_subtasks_plan_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    order: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    target_files: Mapped[list[Any]] = mapped_column(default=list)
    acceptance: Mapped[list[Any]] = mapped_column(default=list)
    required_permissions: Mapped[list[Any]] = mapped_column(default=list)

    plan: Mapped[Plan] = relationship(back_populates="subtasks")


class SubtaskDependency(Base):
    __tablename__ = "subtask_dependencies"

    subtask_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("subtasks.id", ondelete="CASCADE"), primary_key=True)
    depends_on_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("subtasks.id", ondelete="CASCADE"), primary_key=True)


class TaskRun(TimestampMixin, Base):
    """One attempt at executing a task's workflow. Also serves as the durable job queue."""

    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_task_runs_attempt"),
        Index(
            "uq_task_runs_one_active",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
        Index("ix_task_runs_claim", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    reason: Mapped[str] = mapped_column(String(16), default="initial")
    worker_id: Mapped[str | None] = mapped_column(String(128))
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    recovered_from_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    traceparent: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[Task] = relationship(back_populates="runs")


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_task", "task_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"))
    role: Mapped[str] = mapped_column(String(32))
    subtask_key: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    input_summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolInvocation(TimestampMixin, Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        Index("ix_tool_invocations_task", "task_id", "created_at"),
        Index("ix_tool_invocations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    agent_role: Mapped[str] = mapped_column(String(32))
    tool_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    mutating: Mapped[bool] = mapped_column(Boolean, default=False)
    args_redacted: Mapped[dict[str, Any]] = mapped_column(default=dict)
    args_hash: Mapped[str] = mapped_column(String(64))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_preview: Mapped[str | None] = mapped_column(Text)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    output_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(TimestampMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_task", "task_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100), default="text/plain")
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(Text)
    inline_content: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)


class CheckpointRef(TimestampMixin, Base):
    __tablename__ = "checkpoint_refs"
    __table_args__ = (
        UniqueConstraint("thread_id", "checkpoint_id", name="uq_checkpoint_refs"),
        Index("ix_checkpoint_refs_task", "task_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"))
    thread_id: Mapped[str] = mapped_column(String(128))
    checkpoint_ns: Mapped[str] = mapped_column(String(255), default="")
    checkpoint_id: Mapped[str] = mapped_column(String(128))
    node: Mapped[str | None] = mapped_column(String(64))
    step: Mapped[int | None] = mapped_column(Integer)
    restored: Mapped[bool] = mapped_column(Boolean, default=False)


class ApprovalRequest(TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approvals_status", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    requested_by_agent: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    decision: Mapped[ApprovalDecision | None] = relationship(back_populates="request")


class ApprovalDecision(TimestampMixin, Base):
    __tablename__ = "approval_decisions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approval_requests.id", ondelete="CASCADE"), unique=True)
    decision: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"))
    comment: Mapped[str | None] = mapped_column(Text)

    request: Mapped[ApprovalRequest] = relationship(back_populates="decision")


class LLMUsage(TimestampMixin, Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_task", "task_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    agent_role: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(24))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8))
    cost_source: Mapped[str] = mapped_column(String(40), default="estimated")
    pricing_version: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer)


class ExecutionEvent(TimestampMixin, Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_events_task_seq"),
        Index("ix_events_type", "type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16), default="info")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    tool_invocation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tool_invocations.id"))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)


class SpanRecord(Base):
    """Finished OpenTelemetry spans, persisted so the dashboard can render traces without Jaeger."""

    __tablename__ = "spans"
    __table_args__ = (Index("ix_spans_trace", "trace_id"), Index("ix_spans_task", "task_id"))

    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32))
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    service: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Numeric(14, 3))
    status_code: Mapped[str] = mapped_column(String(16))
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)

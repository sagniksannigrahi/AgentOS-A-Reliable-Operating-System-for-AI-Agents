"""Data-access functions. Every function takes an open ``AsyncSession`` whose transaction the
caller controls, so related writes (status change + transition record + event) commit atomically.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from opentelemetry import trace
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EventType, RunReason, RunStatus, ToolCallStatus
from app.domain.errors import LeaseLostError
from app.domain.states import TERMINAL_STATES, TaskStatus, validate_transition
from app.observability.logging import get_logger
from app.persistence.models import (
    AgentRun,
    ApiKey,
    ApprovalRequest,
    Artifact,
    CheckpointRef,
    ExecutionEvent,
    LLMUsage,
    StateTransition,
    Task,
    TaskRun,
    ToolInvocation,
)
from app.security.redaction import redact, redact_text

log = get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash(data: Any) -> str:
    return sha256_text(json.dumps(data, sort_keys=True, default=str, separators=(",", ":")))


def current_trace_ids() -> tuple[str | None, str | None]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


# ------------------------------------------------------------------------------------ events


async def record_event(
    session: AsyncSession,
    task_id: uuid.UUID,
    type: EventType | str,
    *,
    run_id: uuid.UUID | None = None,
    status: str = "info",
    actor: str = "system",
    message: str = "",
    data: dict[str, Any] | None = None,
    agent_run_id: uuid.UUID | None = None,
    tool_invocation_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> int | None:
    """Append an event. Returns its id, or ``None`` if the idempotency key was already used."""
    if idempotency_key is not None:
        exists = await session.scalar(
            select(ExecutionEvent.id).where(ExecutionEvent.idempotency_key == idempotency_key)
        )
        if exists is not None:
            return None
    seq = (
        await session.execute(
            update(Task).where(Task.id == task_id).values(event_seq=Task.event_seq + 1).returning(Task.event_seq)
        )
    ).scalar_one()
    trace_id, span_id = current_trace_ids()
    clean = redact(data or {})
    stmt = (
        pg_insert(ExecutionEvent)
        .values(
            task_id=task_id,
            run_id=run_id,
            seq=seq,
            type=str(type),
            status=status,
            actor=actor,
            message=redact_text(message)[:4000],
            data=clean,
            agent_run_id=agent_run_id,
            tool_invocation_id=tool_invocation_id,
            trace_id=trace_id,
            span_id=span_id,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(ExecutionEvent.id)
    )
    event_id = (await session.execute(stmt)).scalar_one_or_none()
    log.info(
        "event",
        event_type=str(type),
        task_id=str(task_id),
        run_id=str(run_id) if run_id else None,
        status=status,
        actor=actor,
        msg=message[:300],
    )
    return event_id


async def event_exists(session: AsyncSession, idempotency_key: str) -> bool:
    return (
        await session.scalar(select(ExecutionEvent.id).where(ExecutionEvent.idempotency_key == idempotency_key))
    ) is not None


async def count_events(session: AsyncSession, task_id: uuid.UUID, prefix: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ExecutionEvent)
            .where(
                ExecutionEvent.task_id == task_id,
                ExecutionEvent.idempotency_key.like(f"{prefix}%"),
            )
        )
        or 0
    )


# ------------------------------------------------------------------------------- transitions


async def transition_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    target: TaskStatus,
    *,
    actor: str,
    reason: str,
    run_id: uuid.UUID | None = None,
    criteria_passed: bool | None = None,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> bool:
    """Validated status change. Returns False when the task is already in ``target``."""
    task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise LookupError(f"task {task_id} not found")
    current = TaskStatus(task.status)
    if current == target:
        return False
    validate_transition(current, target, criteria_passed=criteria_passed)
    task.status = target.value
    task.version += 1
    if target in (TaskStatus.PAUSED, TaskStatus.RECOVERY_PENDING, TaskStatus.AWAITING_APPROVAL):
        task.paused_from = current.value
    if result is not None:
        task.result = redact(result)
    if error is not None:
        task.error = redact(error)
    if target in TERMINAL_STATES:
        task.completed_at = utcnow()
    trace_id, _ = current_trace_ids()
    session.add(
        StateTransition(
            task_id=task_id,
            run_id=run_id,
            from_status=current.value,
            to_status=target.value,
            actor=actor,
            reason=redact_text(reason)[:2000],
            trace_id=trace_id,
        )
    )
    await session.flush()
    await record_event(
        session,
        task_id,
        EventType.TASK_STATUS_CHANGED,
        run_id=run_id,
        actor=actor,
        message=f"{current.value} -> {target.value}: {reason}",
        data={"from": current.value, "to": target.value},
    )
    terminal_event = {
        TaskStatus.COMPLETED: EventType.TASK_COMPLETED,
        TaskStatus.FAILED: EventType.TASK_FAILED,
        TaskStatus.CANCELLED: EventType.TASK_CANCELLED,
    }.get(target)
    if terminal_event:
        await record_event(
            session,
            task_id,
            terminal_event,
            run_id=run_id,
            actor=actor,
            status="success" if target == TaskStatus.COMPLETED else "error",
            message=reason,
            data={"error": error} if error else {},
        )
    return True


# ------------------------------------------------------------------------------- task runs


async def enqueue_run(
    session: AsyncSession,
    task: Task,
    reason: RunReason,
    *,
    resume_payload: dict[str, Any] | None = None,
    traceparent: str | None = None,
) -> TaskRun:
    attempt = (
        await session.scalar(select(func.coalesce(func.max(TaskRun.attempt), 0)).where(TaskRun.task_id == task.id)) or 0
    ) + 1
    run = TaskRun(
        task_id=task.id,
        attempt=attempt,
        status=RunStatus.QUEUED.value,
        reason=reason.value,
        resume_payload=resume_payload,
        traceparent=traceparent or task.traceparent,
    )
    session.add(run)
    await session.flush()
    await record_event(
        session,
        task.id,
        EventType.RUN_QUEUED,
        run_id=run.id,
        message=f"run #{attempt} queued ({reason.value})",
        data={"attempt": attempt, "reason": reason.value},
    )
    return run


async def active_run(session: AsyncSession, task_id: uuid.UUID) -> TaskRun | None:
    return await session.scalar(
        select(TaskRun).where(
            TaskRun.task_id == task_id,
            TaskRun.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]),
        )
    )


@dataclass(frozen=True)
class ClaimedRun:
    run_id: uuid.UUID
    task_id: uuid.UUID
    attempt: int
    fencing_token: int
    reason: str
    resume_payload: dict[str, Any] | None
    traceparent: str | None
    recovered_from_run_id: uuid.UUID | None


async def claim_run(session: AsyncSession, worker_id: str, lease_seconds: float) -> ClaimedRun | None:
    """Claim the next unit of work.

    Expired leases are taken first: the dead run is marked ABANDONED and a new *recovery* run is
    created for the same task, owned by this worker. Otherwise the oldest QUEUED run is claimed.
    ``FOR UPDATE SKIP LOCKED`` makes concurrent workers pick different rows.
    """
    now = utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)
    expired = await session.scalar(
        select(TaskRun)
        .where(TaskRun.status == RunStatus.RUNNING.value, TaskRun.lease_expires_at < now)
        .order_by(TaskRun.lease_expires_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if expired is not None:
        expired.status = RunStatus.ABANDONED.value
        expired.finished_at = now
        expired.error = {"reason": "lease expired; worker presumed dead", "worker": expired.worker_id}
        await session.flush()
        await record_event(
            session,
            expired.task_id,
            EventType.RUN_ABANDONED,
            run_id=expired.id,
            status="warning",
            message=f"run #{expired.attempt} abandoned: lease expired (worker {expired.worker_id})",
        )
        run = TaskRun(
            task_id=expired.task_id,
            attempt=expired.attempt + 1,
            status=RunStatus.RUNNING.value,
            reason=RunReason.RECOVERY.value,
            worker_id=worker_id,
            fencing_token=expired.fencing_token + 1,
            lease_expires_at=lease_until,
            heartbeat_at=now,
            started_at=now,
            recovered_from_run_id=expired.id,
            traceparent=expired.traceparent,
            resume_payload=expired.resume_payload,
        )
        session.add(run)
        await session.flush()
        return ClaimedRun(
            run.id,
            run.task_id,
            run.attempt,
            run.fencing_token,
            run.reason,
            run.resume_payload,
            run.traceparent,
            expired.id,
        )
    queued = await session.scalar(
        select(TaskRun)
        .where(TaskRun.status == RunStatus.QUEUED.value)
        .order_by(TaskRun.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if queued is None:
        return None
    queued.status = RunStatus.RUNNING.value
    queued.worker_id = worker_id
    queued.fencing_token += 1
    queued.lease_expires_at = lease_until
    queued.heartbeat_at = now
    queued.started_at = now
    await session.flush()
    return ClaimedRun(
        queued.id,
        queued.task_id,
        queued.attempt,
        queued.fencing_token,
        queued.reason,
        queued.resume_payload,
        queued.traceparent,
        queued.recovered_from_run_id,
    )


async def heartbeat(session: AsyncSession, run_id: uuid.UUID, fencing_token: int, lease_seconds: float) -> bool:
    now = utcnow()
    row = await session.execute(
        update(TaskRun)
        .where(
            TaskRun.id == run_id,
            TaskRun.fencing_token == fencing_token,
            TaskRun.status == RunStatus.RUNNING.value,
        )
        .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))
        .returning(TaskRun.id)
    )
    return row.scalar_one_or_none() is not None


async def guard_lease(session: AsyncSession, run_id: uuid.UUID, fencing_token: int) -> None:
    """Fence a write: raise unless this run is still RUNNING with our token.

    ``FOR SHARE`` blocks a concurrent claimer from abandoning the run until this transaction
    commits, so a write is either fully made by the lease owner or not at all.
    """
    row = (
        await session.execute(
            select(TaskRun.status, TaskRun.fencing_token).where(TaskRun.id == run_id).with_for_update(read=True)
        )
    ).one_or_none()
    if row is None or row.status != RunStatus.RUNNING.value or row.fencing_token != fencing_token:
        raise LeaseLostError("lease lost", details={"run_id": str(run_id), "status": row.status if row else None})


async def finish_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    fencing_token: int,
    status: RunStatus,
    error: dict[str, Any] | None = None,
) -> bool:
    res = await session.execute(
        update(TaskRun)
        .where(
            TaskRun.id == run_id,
            TaskRun.fencing_token == fencing_token,
            TaskRun.status == RunStatus.RUNNING.value,
        )
        .values(status=status.value, finished_at=utcnow(), error=redact(error) if error else None)
        .returning(TaskRun.task_id)
    )
    return res.scalar_one_or_none() is not None


async def count_recoveries(session: AsyncSession, task_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(TaskRun)
            .where(TaskRun.task_id == task_id, TaskRun.reason == RunReason.RECOVERY.value)
        )
        or 0
    )


# ------------------------------------------------------------------------------ agent runs


async def start_agent_run(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    role: str,
    subtask_key: str | None,
    attempt: int,
    idempotency_key: str,
    input_summary: dict[str, Any],
) -> tuple[uuid.UUID, int]:
    """Create (or, on replay after a crash, reopen) an agent run. Returns (id, replay_count)."""
    trace_id, span_id = current_trace_ids()
    stmt = (
        pg_insert(AgentRun)
        .values(
            id=uuid.uuid4(),
            task_id=task_id,
            run_id=run_id,
            role=role,
            subtask_key=subtask_key,
            attempt=attempt,
            status="RUNNING",
            idempotency_key=idempotency_key,
            input_summary=redact(input_summary),
            trace_id=trace_id,
            span_id=span_id,
        )
        .on_conflict_do_update(
            index_elements=["idempotency_key"],
            set_={
                "replay_count": AgentRun.replay_count + 1,
                "status": "RUNNING",
                "run_id": run_id,
                "started_at": func.now(),
                "finished_at": None,
                "error": None,
                "trace_id": trace_id,
                "span_id": span_id,
            },
        )
        .returning(AgentRun.id, AgentRun.replay_count)
    )
    row = (await session.execute(stmt)).one()
    return row.id, row.replay_count


async def finish_agent_run(
    session: AsyncSession,
    agent_run_id: uuid.UUID,
    *,
    status: str,
    latency_ms: int,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id == agent_run_id)
        .values(
            status=status,
            latency_ms=latency_ms,
            result=redact(result) if result is not None else None,
            error=redact(error) if error is not None else None,
            finished_at=utcnow(),
        )
    )


# ----------------------------------------------------------------------------------- usage


async def record_usage(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    agent_run_id: uuid.UUID | None,
    agent_role: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    source: str,
    cost_usd: Decimal,
    cost_source: str,
    pricing_version: str,
    latency_ms: int,
) -> None:
    session.add(
        LLMUsage(
            task_id=task_id,
            run_id=run_id,
            agent_run_id=agent_run_id,
            agent_role=agent_role,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            source=source,
            cost_usd=cost_usd,
            cost_source=cost_source,
            pricing_version=pricing_version,
            latency_ms=latency_ms,
        )
    )
    await session.flush()


# ------------------------------------------------------------------------------- artifacts

INLINE_ARTIFACT_LIMIT = 64_000


async def save_artifact(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID | None,
    kind: str,
    name: str,
    content: str,
    artifact_root: Path,
    idempotency_key: str,
    content_type: str = "text/plain",
    meta: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, bool]:
    """Store an artifact once. Returns (artifact_id, created)."""
    existing = await session.scalar(select(Artifact.id).where(Artifact.idempotency_key == idempotency_key))
    if existing is not None:
        return existing, False
    content = redact_text(content)
    digest = sha256_text(content)
    size = len(content.encode("utf-8"))
    storage_path: str | None = None
    inline: str | None = content
    if size > INLINE_ARTIFACT_LIMIT:
        target_dir = artifact_root / str(task_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:80]
        target = target_dir / f"{digest[:16]}-{safe_name}"
        target.write_text(content, encoding="utf-8")
        storage_path = str(target)
        inline = None
    art = Artifact(
        task_id=task_id,
        run_id=run_id,
        kind=kind,
        name=name,
        content_type=content_type,
        sha256=digest,
        size_bytes=size,
        storage_path=storage_path,
        inline_content=inline,
        meta=redact(meta or {}),
        idempotency_key=idempotency_key,
    )
    session.add(art)
    await session.flush()
    await record_event(
        session,
        task_id,
        EventType.ARTIFACT_CREATED,
        run_id=run_id,
        message=f"artifact {kind}:{name}",
        data={"artifact_id": str(art.id), "kind": kind, "name": name, "sha256": digest},
    )
    return art.id, True


def read_artifact_content(art: Artifact) -> str:
    if art.inline_content is not None:
        return art.inline_content
    if art.storage_path:
        return Path(art.storage_path).read_text(encoding="utf-8")
    return ""


# ----------------------------------------------------------------------------- checkpoints


async def record_checkpoint(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    thread_id: str,
    checkpoint_id: str,
    checkpoint_ns: str = "",
    node: str | None,
    step: int | None,
    restored: bool = False,
) -> bool:
    stmt = (
        pg_insert(CheckpointRef)
        .values(
            task_id=task_id,
            run_id=run_id,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
            node=node,
            step=step,
            restored=restored,
        )
        .on_conflict_do_nothing(constraint="uq_checkpoint_refs")
        .returning(CheckpointRef.id)
    )
    created = (await session.execute(stmt)).scalar_one_or_none() is not None
    if restored:
        await session.execute(
            update(CheckpointRef)
            .where(CheckpointRef.thread_id == thread_id, CheckpointRef.checkpoint_id == checkpoint_id)
            .values(restored=True)
        )
    return created


# ------------------------------------------------------------------------------- approvals


async def get_or_create_approval(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    run_id: uuid.UUID | None,
    kind: str,
    reason: str,
    payload: dict[str, Any],
    requested_by_agent: str,
    idempotency_key: str,
    ttl_seconds: float,
) -> tuple[ApprovalRequest, bool]:
    existing = await session.scalar(select(ApprovalRequest).where(ApprovalRequest.idempotency_key == idempotency_key))
    if existing is not None:
        return existing, False
    req = ApprovalRequest(
        task_id=task_id,
        run_id=run_id,
        kind=kind,
        reason=redact_text(reason),
        payload=redact(payload),
        requested_by_agent=requested_by_agent,
        idempotency_key=idempotency_key,
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    )
    session.add(req)
    await session.flush()
    await record_event(
        session,
        task_id,
        EventType.APPROVAL_REQUESTED,
        run_id=run_id,
        actor=requested_by_agent,
        status="warning",
        message=f"human approval requested ({kind}): {reason}",
        data={"approval_id": str(req.id), "kind": kind},
    )
    return req, True


# ---------------------------------------------------------------------------------- api keys

API_KEY_PREFIX = "aos_"
ALL_SCOPES = ("tasks:read", "tasks:write", "approvals:read", "approvals:decide", "admin")


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def create_api_key(
    session: AsyncSession, name: str, scopes: list[str], plaintext: str | None = None
) -> tuple[ApiKey, str]:
    unknown = set(scopes) - set(ALL_SCOPES)
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")
    plaintext = plaintext or API_KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(name=name, key_prefix=plaintext[:10], key_hash=hash_api_key(plaintext), scopes=scopes)
    session.add(key)
    await session.flush()
    return key, plaintext


async def authenticate_api_key(session: AsyncSession, plaintext: str) -> ApiKey | None:
    key = await session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(plaintext), ApiKey.revoked_at.is_(None))
    )
    if key is not None:
        key.last_used_at = utcnow()
    return key


# -------------------------------------------------------------------------- tool invocations


async def abandon_inflight_tool_calls(session: AsyncSession, task_id: uuid.UUID, current_run_id: uuid.UUID) -> int:
    """Mark write-ahead INTENT records from dead runs as ABANDONED (outcome unknown).

    The tool gateway reconciles them if the same call is replayed; anything left ABANDONED after
    the task finishes is reported as a possible-but-unverified side effect.
    """
    res = await session.execute(
        update(ToolInvocation)
        .where(
            ToolInvocation.task_id == task_id,
            ToolInvocation.status == ToolCallStatus.INTENT.value,
            ToolInvocation.run_id != current_run_id,
        )
        .values(status=ToolCallStatus.ABANDONED.value, decision_reason="run died before outcome was recorded")
        .returning(ToolInvocation.id)
    )
    return len(res.scalars().all())


async def release_lease(session: AsyncSession, run_id: uuid.UUID, fencing_token: int) -> None:
    """Graceful shutdown: expire our lease now so another worker recovers the run immediately."""
    await session.execute(
        update(TaskRun)
        .where(TaskRun.id == run_id, TaskRun.fencing_token == fencing_token, TaskRun.status == RunStatus.RUNNING.value)
        .values(lease_expires_at=utcnow())
    )

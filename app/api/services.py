"""Use-case functions behind the REST endpoints. Each runs in one explicit transaction."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AppState, Principal
from app.api.errors import APIError
from app.config.settings import LLMProviderName
from app.domain.enums import ApprovalStatus, EventType, RunReason, RunStatus
from app.domain.states import RESUMABLE_STATES, TERMINAL_STATES, TaskStatus
from app.observability import metrics
from app.observability.tracing import current_traceparent, trace_id_of
from app.orchestration.faults import validate_fault_specs
from app.persistence.models import ApprovalDecision, ApprovalRequest, Task
from app.persistence.repository import (
    active_run,
    enqueue_run,
    record_event,
    stable_hash,
    transition_task,
    utcnow,
)
from app.schemas.api import TaskCreate
from app.security.commands import CommandNotAllowedError, validate_command
from app.tools.workspace import seed_exists


def _validate_task_request(state: AppState, payload: TaskCreate) -> None:
    s = state.settings
    if not seed_exists(s.seed_repos_dir, payload.workspace_seed):
        raise APIError(422, "unknown_workspace_seed", f"workspace seed '{payload.workspace_seed}' does not exist")
    for c in payload.acceptance_criteria:
        if c.kind == "tests_pass":
            try:
                validate_command(c.command)
            except CommandNotAllowedError as exc:
                raise APIError(422, "command_not_allowed", f"criterion {c.id}: {exc.message}", exc.details) from exc
    ids = [c.id for c in payload.acceptance_criteria]
    if len(ids) != len(set(ids)):
        raise APIError(422, "validation_error", "acceptance criterion ids must be unique")
    opts = payload.options
    if opts.uses_evaluation_features() and not s.evaluation_mode:
        raise APIError(
            403,
            "evaluation_features_disabled",
            "workflow overrides, scripted scenarios and fault injection require AGENTOS_EVALUATION_MODE=true",
        )
    if opts.llm_scenario and s.llm_provider != LLMProviderName.FAKE:
        raise APIError(422, "validation_error", "llm_scenario requires the fake LLM provider")
    if opts.llm_scenario:
        from app.evaluation.scenarios import SCENARIOS

        if opts.llm_scenario not in SCENARIOS:
            raise APIError(422, "unknown_scenario", f"unknown scenario '{opts.llm_scenario}'")
    try:
        validate_fault_specs(opts.faults)
    except ValueError as exc:
        raise APIError(422, "validation_error", str(exc)) from exc


async def create_task(
    state: AppState, payload: TaskCreate, principal: Principal, idempotency_key: str | None
) -> tuple[Task, bool]:
    """Returns (task, created). Re-sending the same Idempotency-Key returns the original task."""
    _validate_task_request(state, payload)
    request = payload.model_dump(mode="json")
    request_hash = stable_hash(request)
    if idempotency_key:
        async with state.db.session() as s:
            existing = await s.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise APIError(409, "idempotency_conflict", "Idempotency-Key reused with a different request")
                return existing, False
    if not await state.redis.allow(f"tasks:{principal.id}", state.settings.task_create_rate_per_minute):
        raise APIError(429, "rate_limited", "too many tasks created; try again later")
    traceparent = current_traceparent()
    try:
        async with state.db.session() as s:
            task = Task(
                title=payload.title,
                description=payload.description,
                status=TaskStatus.CREATED.value,
                request=request,
                workspace_seed=payload.workspace_seed,
                created_by=principal.id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                traceparent=traceparent,
                trace_id=trace_id_of(traceparent),
            )
            s.add(task)
            await s.flush()
            await record_event(
                s,
                task.id,
                EventType.TASK_CREATED,
                actor=f"api:{principal.name}",
                message=f"task created: {payload.title}",
                data={"seed": payload.workspace_seed, "criteria": len(payload.acceptance_criteria)},
            )
            await enqueue_run(s, task, RunReason.INITIAL, traceparent=traceparent)
    except IntegrityError as exc:
        raise APIError(409, "conflict", "task could not be created (duplicate idempotency key?)") from exc
    metrics.TASKS_CREATED.inc()
    await state.redis.notify_work()
    return task, True


async def cancel_task(state: AppState, task_id: uuid.UUID, principal: Principal, reason: str) -> dict[str, Any]:
    async with state.db.session() as s:
        task = await s.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            raise APIError(404, "not_found", "task not found")
        if TaskStatus(task.status) in TERMINAL_STATES:
            raise APIError(409, "invalid_state", f"task is already {task.status}")
        if task.cancel_requested_at is None:
            task.cancel_requested_at = utcnow()
            await record_event(
                s,
                task.id,
                EventType.CANCEL_REQUESTED,
                actor=f"api:{principal.name}",
                status="warning",
                message=f"cancellation requested: {reason}",
            )
        run = await active_run(s, task.id)
        if run is None or run.status == RunStatus.QUEUED.value:
            if run is not None:
                run.status = RunStatus.CANCELLED.value
                run.finished_at = utcnow()
            await transition_task(
                s,
                task.id,
                TaskStatus.CANCELLED,
                actor=f"api:{principal.name}",
                reason=reason,
                run_id=run.id if run else None,
            )
            for appr in (
                await s.scalars(
                    select(ApprovalRequest).where(
                        ApprovalRequest.task_id == task.id,
                        ApprovalRequest.status == ApprovalStatus.PENDING.value,
                    )
                )
            ).all():
                appr.status = ApprovalStatus.EXPIRED.value
            metrics.TASKS_FINISHED.labels(status="CANCELLED").inc()
            return {"task_id": str(task.id), "status": "CANCELLED", "accepted": True}
    return {"task_id": str(task_id), "status": "CANCEL_REQUESTED", "accepted": True}


async def resume_task(state: AppState, task_id: uuid.UUID, principal: Principal, reason: str) -> dict[str, Any]:
    async with state.db.session() as s:
        task = await s.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            raise APIError(404, "not_found", "task not found")
        if TaskStatus(task.status) not in RESUMABLE_STATES:
            raise APIError(
                409,
                "invalid_state",
                f"only {sorted(x.value for x in RESUMABLE_STATES)} tasks can be resumed (task is {task.status})",
            )
        if await active_run(s, task.id) is not None:
            raise APIError(409, "invalid_state", "task already has an active run")
        run = await enqueue_run(
            s,
            task,
            RunReason.RESUME,
            resume_payload={"reason": reason, "by": principal.name},
        )
    await state.redis.notify_work()
    return {"task_id": str(task_id), "run_id": str(run.id), "status": "RESUME_QUEUED"}


async def decide_approval(
    state: AppState, approval_id: uuid.UUID, principal: Principal, decision: str, comment: str | None
) -> ApprovalRequest:
    expired = False
    async with state.db.session() as s:
        req = await s.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update())
        if req is None:
            raise APIError(404, "not_found", "approval request not found")
        if req.status != ApprovalStatus.PENDING.value:
            raise APIError(409, "already_decided", f"approval is {req.status}")
        task = await s.scalar(select(Task).where(Task.id == req.task_id).with_for_update())
        assert task is not None
        if TaskStatus(task.status) in TERMINAL_STATES:
            raise APIError(409, "invalid_state", f"task is already {task.status}")
        if req.expires_at < utcnow():
            req.status = ApprovalStatus.EXPIRED.value
            expired = True
        else:
            if task.created_by == principal.id and not state.settings.allow_self_approval:
                raise APIError(403, "self_approval_forbidden", "the task creator cannot approve their own task")
            req.status = (ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.REJECTED).value
            s.add(ApprovalDecision(approval_id=req.id, decision=decision, decided_by=principal.id, comment=comment))
            await record_event(
                s,
                task.id,
                EventType.APPROVAL_DECIDED,
                actor=f"human:{principal.name}",
                status="success" if decision == "approve" else "warning",
                message=f"approval {req.kind} {decision}d" + (f": {comment}" if comment else ""),
                data={"approval_id": str(req.id), "decision": decision},
            )
            metrics.APPROVALS.labels(event=decision).inc()
            # Resume the paused workflow. If the run that asked is still finishing its pause,
            # the worker enqueues the continuation itself when it sees the decision.
            if TaskStatus(task.status) == TaskStatus.AWAITING_APPROVAL and await active_run(s, task.id) is None:
                await enqueue_run(
                    s,
                    task,
                    RunReason.APPROVAL,
                    resume_payload={"approval_id": str(req.id), "decision": decision, "comment": comment},
                )
    if expired:
        raise APIError(409, "approval_expired", "approval request has expired")
    await state.redis.notify_work()
    return req

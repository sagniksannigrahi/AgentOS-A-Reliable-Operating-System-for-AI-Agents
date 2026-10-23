"""/api/v1/tasks endpoints."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from sqlalchemy import func, or_, select

from app.api import services
from app.api.deps import AppState, Principal, get_state, require_scopes
from app.api.errors import APIError
from app.domain.states import TaskStatus
from app.persistence.models import (
    AgentRun,
    ApprovalRequest,
    Artifact,
    CheckpointRef,
    ExecutionEvent,
    LLMUsage,
    Plan,
    SpanRecord,
    StateTransition,
    Task,
    TaskRun,
    ToolInvocation,
)
from app.persistence.repository import read_artifact_content
from app.schemas.api import (
    AgentRunOut,
    ArtifactContent,
    ArtifactOut,
    CancelIn,
    CheckpointOut,
    EventOut,
    EventPage,
    Page,
    ResumeIn,
    SpanOut,
    TaskCreate,
    TaskDetail,
    TaskRunOut,
    TaskSummary,
    ToolInvocationOut,
    TransitionOut,
    UsageTotals,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100_000)]
read_scope = require_scopes("tasks:read")
write_scope = require_scopes("tasks:write")


async def _get_task(state: AppState, task_id: uuid.UUID) -> Task:
    async with state.db.session() as s:
        task = await s.get(Task, task_id)
    if task is None:
        raise APIError(404, "not_found", "task not found")
    return task


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TaskDetail)
async def create_task(
    payload: TaskCreate,
    response: Response,
    state: AppState = Depends(get_state),
    principal: Principal = Depends(write_scope),
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> TaskDetail:
    task, created = await services.create_task(state, payload, principal, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/api/v1/tasks/{task.id}"
    return await _detail(state, task.id)


@router.get("", response_model=Page[TaskSummary])
async def list_tasks(
    state: AppState = Depends(get_state),
    _: Principal = Depends(read_scope),
    status_filter: Annotated[list[TaskStatus] | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> Page[TaskSummary]:
    stmt = select(Task)
    if status_filter:
        stmt = stmt.where(Task.status.in_([s.value for s in status_filter]))
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        stmt = stmt.where(or_(Task.title.ilike(like), Task.description.ilike(like)))
    async with state.db.session() as s:
        total = await s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = (await s.scalars(stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset))).all()
    return Page(items=[TaskSummary.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


async def _usage(s: Any, task_id: uuid.UUID) -> UsageTotals:
    rows = (
        await s.execute(
            select(
                LLMUsage.agent_role,
                LLMUsage.source,
                func.count(),
                func.sum(LLMUsage.input_tokens),
                func.sum(LLMUsage.output_tokens),
                func.sum(LLMUsage.cost_usd),
            )
            .where(LLMUsage.task_id == task_id)
            .group_by(LLMUsage.agent_role, LLMUsage.source)
        )
    ).all()
    u = UsageTotals()
    for role, source, calls, inp, out, cost in rows:
        inp, out, cost = int(inp or 0), int(out or 0), Decimal(cost or 0)
        u.calls += calls
        u.input_tokens += inp
        u.output_tokens += out
        u.cost_usd += cost
        if source == "provider_reported":
            u.provider_reported_tokens += inp + out
            u.cost_usd_from_reported_tokens += cost
        else:
            u.estimated_tokens += inp + out
            u.cost_usd_from_estimated_tokens += cost
        r = u.by_role.setdefault(role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        r["calls"] += calls
        r["input_tokens"] += inp
        r["output_tokens"] += out
        r["cost_usd"] = round(r["cost_usd"] + float(cost), 8)
    return u


async def _detail(state: AppState, task_id: uuid.UUID) -> TaskDetail:
    async with state.db.session() as s:
        task = await s.get(Task, task_id)
        if task is None:
            raise APIError(404, "not_found", "task not found")
        plan = None
        if task.current_plan_version is not None:
            p = await s.scalar(select(Plan).where(Plan.task_id == task_id, Plan.version == task.current_plan_version))
            if p is not None:
                plan = {"version": p.version, "reason": p.reason, **p.content}
        runs = (await s.scalars(select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.attempt))).all()
        ck = await s.scalar(
            select(CheckpointRef).where(CheckpointRef.task_id == task_id).order_by(CheckpointRef.id.desc()).limit(1)
        )
        pending = await s.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.task_id == task_id, ApprovalRequest.status == "PENDING")
        )
        counts: dict[str, int] = {}
        for name, model in (
            ("events", ExecutionEvent),
            ("agent_runs", AgentRun),
            ("tool_calls", ToolInvocation),
            ("artifacts", Artifact),
            ("checkpoints", CheckpointRef),
        ):
            counts[name] = int(
                await s.scalar(select(func.count()).select_from(model).where(model.task_id == task_id)) or 0
            )
        counts["tool_denied"] = int(
            await s.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .where(ToolInvocation.task_id == task_id, ToolInvocation.status == "DENIED")
            )
            or 0
        )
        counts["retries"] = int(
            await s.scalar(
                select(func.count())
                .select_from(ExecutionEvent)
                .where(ExecutionEvent.task_id == task_id, ExecutionEvent.type == "retry.initiated")
            )
            or 0
        )
        counts["recoveries"] = sum(1 for r in runs if r.reason == "recovery")
        usage = await _usage(s, task_id)
    computed = {"plan", "runs", "usage", "latest_checkpoint", "pending_approvals", "counts"}
    detail = TaskDetail.model_validate({k: getattr(task, k) for k in TaskDetail.model_fields if k not in computed})
    detail.plan = plan
    detail.runs = [TaskRunOut.model_validate(r) for r in runs]
    detail.usage = usage
    detail.latest_checkpoint = (
        {"checkpoint_id": ck.checkpoint_id, "node": ck.node, "step": ck.step, "created_at": ck.created_at.isoformat()}
        if ck
        else None
    )
    detail.pending_approvals = int(pending or 0)
    detail.counts = counts
    return detail


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(
    task_id: uuid.UUID, state: AppState = Depends(get_state), _: Principal = Depends(read_scope)
) -> TaskDetail:
    return await _detail(state, task_id)


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_task(
    task_id: uuid.UUID,
    response: Response,
    body: Annotated[CancelIn | None, Body()] = None,
    state: AppState = Depends(get_state),
    principal: Principal = Depends(write_scope),
) -> dict[str, Any]:
    result = await services.cancel_task(state, task_id, principal, (body or CancelIn()).reason)
    if result["status"] == "CANCELLED":
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_task(
    task_id: uuid.UUID,
    body: Annotated[ResumeIn | None, Body()] = None,
    state: AppState = Depends(get_state),
    principal: Principal = Depends(write_scope),
) -> dict[str, Any]:
    return await services.resume_task(state, task_id, principal, (body or ResumeIn()).reason)


@router.get("/{task_id}/events", response_model=EventPage)
async def list_events(
    task_id: uuid.UUID,
    state: AppState = Depends(get_state),
    _: Principal = Depends(read_scope),
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    type_prefix: Annotated[str | None, Query(max_length=48)] = None,
) -> EventPage:
    await _get_task(state, task_id)
    stmt = select(ExecutionEvent).where(ExecutionEvent.task_id == task_id, ExecutionEvent.seq > after_seq)
    if type_prefix:
        stmt = stmt.where(ExecutionEvent.type.startswith(type_prefix))
    async with state.db.session() as s:
        rows = (await s.scalars(stmt.order_by(ExecutionEvent.seq).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return EventPage(
        items=[EventOut.model_validate(r) for r in rows],
        next_after_seq=rows[-1].seq if more and rows else None,
    )


@router.get("/{task_id}/agents", response_model=Page[AgentRunOut])
async def list_agent_runs(
    task_id: uuid.UUID,
    state: AppState = Depends(get_state),
    _: Principal = Depends(read_scope),
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[AgentRunOut]:
    await _get_task(state, task_id)
    async with state.db.session() as s:
        total = await s.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task_id)) or 0
        rows = (
            await s.scalars(
                select(AgentRun)
                .where(AgentRun.task_id == task_id)
                .order_by(AgentRun.started_at)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        ids = [r.id for r in rows]
        tool_counts = (
            dict(
                (
                    await s.execute(
                        select(ToolInvocation.agent_run_id, func.count())
                        .where(ToolInvocation.agent_run_id.in_(ids))
                        .group_by(ToolInvocation.agent_run_id)
                    )
                ).all()
            )
            if ids
            else {}
        )
        token_counts = (
            dict(
                (
                    await s.execute(
                        select(LLMUsage.agent_run_id, func.sum(LLMUsage.input_tokens + LLMUsage.output_tokens))
                        .where(LLMUsage.agent_run_id.in_(ids))
                        .group_by(LLMUsage.agent_run_id)
                    )
                ).all()
            )
            if ids
            else {}
        )
    items = []
    for r in rows:
        out = AgentRunOut.model_validate(r)
        out.tool_calls = int(tool_counts.get(r.id, 0))
        out.tokens = int(token_counts.get(r.id, 0) or 0)
        items.append(out)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{task_id}/tools", response_model=Page[ToolInvocationOut])
async def list_tool_invocations(
    task_id: uuid.UUID,
    state: AppState = Depends(get_state),
    _: Principal = Depends(read_scope),
    status_filter: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[ToolInvocationOut]:
    await _get_task(state, task_id)
    stmt = select(ToolInvocation).where(ToolInvocation.task_id == task_id)
    if status_filter:
        stmt = stmt.where(ToolInvocation.status == status_filter.upper())
    async with state.db.session() as s:
        total = await s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = (await s.scalars(stmt.order_by(ToolInvocation.created_at).limit(limit).offset(offset))).all()
    return Page(items=[ToolInvocationOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{task_id}/artifacts", response_model=Page[ArtifactOut])
async def list_artifacts(
    task_id: uuid.UUID,
    state: AppState = Depends(get_state),
    _: Principal = Depends(read_scope),
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[ArtifactOut]:
    await _get_task(state, task_id)
    async with state.db.session() as s:
        total = await s.scalar(select(func.count()).select_from(Artifact).where(Artifact.task_id == task_id)) or 0
        rows = (
            await s.scalars(
                select(Artifact)
                .where(Artifact.task_id == task_id)
                .order_by(Artifact.created_at)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return Page(items=[ArtifactOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{task_id}/artifacts/{artifact_id}", response_model=ArtifactContent)
async def get_artifact(
    task_id: uuid.UUID,
    artifact_id: uuid.UUID,
    state: AppState = Depends(get_state),
    _: Principal = Depends(read_scope),
) -> ArtifactContent:
    async with state.db.session() as s:
        art = await s.scalar(select(Artifact).where(Artifact.id == artifact_id, Artifact.task_id == task_id))
    if art is None:
        raise APIError(404, "not_found", "artifact not found")
    out = ArtifactContent.model_validate(
        {**ArtifactOut.model_validate(art).model_dump(), "content": read_artifact_content(art)}
    )
    return out


@router.get("/{task_id}/transitions", response_model=list[TransitionOut])
async def list_transitions(
    task_id: uuid.UUID, state: AppState = Depends(get_state), _: Principal = Depends(read_scope)
) -> list[TransitionOut]:
    await _get_task(state, task_id)
    async with state.db.session() as s:
        rows = (
            await s.scalars(
                select(StateTransition).where(StateTransition.task_id == task_id).order_by(StateTransition.id)
            )
        ).all()
    return [TransitionOut.model_validate(r) for r in rows]


@router.get("/{task_id}/checkpoints", response_model=list[CheckpointOut])
async def list_checkpoints(
    task_id: uuid.UUID, state: AppState = Depends(get_state), _: Principal = Depends(read_scope)
) -> list[CheckpointOut]:
    await _get_task(state, task_id)
    async with state.db.session() as s:
        rows = (
            await s.scalars(select(CheckpointRef).where(CheckpointRef.task_id == task_id).order_by(CheckpointRef.id))
        ).all()
    return [CheckpointOut.model_validate(r) for r in rows]


@router.get("/{task_id}/trace", response_model=list[SpanOut])
async def get_trace(
    task_id: uuid.UUID, state: AppState = Depends(get_state), _: Principal = Depends(read_scope)
) -> list[SpanOut]:
    task = await _get_task(state, task_id)
    conds = [SpanRecord.task_id == task_id]
    if task.trace_id:
        conds.append(SpanRecord.trace_id == task.trace_id)
    async with state.db.session() as s:
        rows = (
            await s.scalars(select(SpanRecord).where(or_(*conds)).order_by(SpanRecord.start_time).limit(2000))
        ).all()
    return [SpanOut.model_validate(r) for r in rows]

"""Approvals, health, stats and metadata endpoints."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app.api import services
from app.api.deps import AppState, Principal, get_state, require_scopes
from app.config.settings import PROJECT_ROOT
from app.persistence.models import (
    ApprovalDecision,
    ApprovalRequest,
    ExecutionEvent,
    LLMUsage,
    Task,
    TaskRun,
    ToolInvocation,
)
from app.schemas.api import ApprovalDecisionIn, ApprovalOut, Page
from app.security.commands import ALLOWLIST
from app.tools.workspace import list_seeds

approvals = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])
health = APIRouter(tags=["health"])
meta = APIRouter(prefix="/api/v1", tags=["meta"])


def _approval_out(req: ApprovalRequest, dec: ApprovalDecision | None, title: str | None) -> ApprovalOut:
    cols = (
        "id",
        "task_id",
        "run_id",
        "kind",
        "status",
        "reason",
        "payload",
        "requested_by_agent",
        "expires_at",
        "created_at",
    )
    out = ApprovalOut.model_validate({c: getattr(req, c) for c in cols})
    out.task_title = title
    if dec is not None:
        out.decision, out.decided_by, out.decided_at, out.comment = (
            dec.decision,
            dec.decided_by,
            dec.created_at,
            dec.comment,
        )
    return out


@approvals.get("", response_model=Page[ApprovalOut])
async def list_approvals(
    state: AppState = Depends(get_state),
    _: Principal = Depends(require_scopes("approvals:read")),
    status: Annotated[str | None, Query(max_length=16)] = "PENDING",
    task_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ApprovalOut]:
    stmt = (
        select(ApprovalRequest, ApprovalDecision, Task.title)
        .join(Task, Task.id == ApprovalRequest.task_id)
        .outerjoin(ApprovalDecision, ApprovalDecision.approval_id == ApprovalRequest.id)
    )
    if status and status.upper() != "ALL":
        stmt = stmt.where(ApprovalRequest.status == status.upper())
    if task_id:
        stmt = stmt.where(ApprovalRequest.task_id == task_id)
    async with state.db.session() as s:
        total = await s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = (await s.execute(stmt.order_by(ApprovalRequest.created_at.desc()).limit(limit).offset(offset))).all()
    return Page(items=[_approval_out(r, d, t) for r, d, t in rows], total=total, limit=limit, offset=offset)


@approvals.post("/{approval_id}/decision", response_model=ApprovalOut)
async def decide(
    approval_id: uuid.UUID,
    body: ApprovalDecisionIn,
    state: AppState = Depends(get_state),
    principal: Principal = Depends(require_scopes("approvals:decide")),
) -> ApprovalOut:
    await services.decide_approval(state, approval_id, principal, body.decision, body.comment)
    async with state.db.session() as s:
        row = (
            await s.execute(
                select(ApprovalRequest, ApprovalDecision, Task.title)
                .join(Task, Task.id == ApprovalRequest.task_id)
                .outerjoin(ApprovalDecision, ApprovalDecision.approval_id == ApprovalRequest.id)
                .where(ApprovalRequest.id == approval_id)
            )
        ).one()
    return _approval_out(*row)


# ----------------------------------------------------------------------------------- health


@health.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/health/ready")
async def ready(state: AppState = Depends(get_state)) -> JSONResponse:
    checks: dict[str, Any] = {}
    ok = True
    try:
        async with state.db.session() as s:
            await s.execute(text("SELECT 1"))
            checks["database"] = "ok"
            version = await s.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            head = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_current_head()
            checks["migrations"] = "ok" if version == head else f"pending (db={version}, head={head})"
            ok &= version == head
            ck = await s.scalar(text("SELECT to_regclass('public.checkpoints') IS NOT NULL"))
            checks["checkpointer"] = "ok" if ck else "missing tables (run: agentos migrate)"
            ok &= bool(ck)
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        ok = False
    redis_ok = await state.redis.ping()
    checks["redis"] = "ok" if redis_ok else ("disabled" if state.redis.client is None else "unavailable (degraded)")
    body = {"status": "ready" if ok else "not_ready", "degraded": not redis_ok, "checks": checks}
    return JSONResponse(body, status_code=200 if ok else 503)


# ------------------------------------------------------------------------------------ stats


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


@meta.get("/stats")
async def stats(
    state: AppState = Depends(get_state), _: Principal = Depends(require_scopes("tasks:read"))
) -> dict[str, Any]:
    async with state.db.session() as s:
        by_status = dict((await s.execute(select(Task.status, func.count()).group_by(Task.status))).all())
        total = sum(by_status.values())
        completed, failed = by_status.get("COMPLETED", 0), by_status.get("FAILED", 0)
        finished = completed + failed + by_status.get("CANCELLED", 0)
        ev = dict(
            (
                await s.execute(
                    select(ExecutionEvent.type, func.count())
                    .where(
                        ExecutionEvent.type.in_(
                            [
                                "retry.initiated",
                                "loop.detected",
                                "approval.requested",
                                "conflict.detected",
                                "tool.denied",
                                "checkpoint.written",
                                "checkpoint.restored",
                                "fault.injected",
                                "budget.exceeded",
                                "replan.initiated",
                            ]
                        )
                    )
                    .group_by(ExecutionEvent.type)
                )
            ).all()
        )
        tool = dict(
            (await s.execute(select(ToolInvocation.status, func.count()).group_by(ToolInvocation.status))).all()
        )
        tool_total = sum(tool.values())
        rec_tasks = (
            (
                await s.execute(
                    select(Task.status).where(Task.id.in_(select(TaskRun.task_id).where(TaskRun.reason == "recovery")))
                )
            )
            .scalars()
            .all()
        )
        rec_ok = sum(1 for st in rec_tasks if st == "COMPLETED")
        rec_done = sum(1 for st in rec_tasks if st in ("COMPLETED", "FAILED", "CANCELLED"))
        e2e = await s.scalar(
            select(func.avg(func.extract("epoch", Task.completed_at - Task.created_at))).where(
                Task.completed_at.is_not(None)
            )
        )
        usage_rows = (
            await s.execute(
                select(
                    LLMUsage.source,
                    func.sum(LLMUsage.input_tokens),
                    func.sum(LLMUsage.output_tokens),
                    func.sum(LLMUsage.cost_usd),
                    func.count(),
                ).group_by(LLMUsage.source)
            )
        ).all()
        agent_lat = (
            await s.execute(
                text(
                    "SELECT role, count(*), avg(latency_ms), percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
                    "FROM agent_runs WHERE latency_ms IS NOT NULL GROUP BY role"
                )
            )
        ).all()
        recent = (
            await s.execute(
                text(
                    "SELECT date_trunc('hour', created_at) AS h, status, count(*) FROM tasks "
                    "WHERE created_at > now() - interval '48 hours' GROUP BY 1, 2 ORDER BY 1"
                )
            )
        ).all()
    usage = {
        src: {"input_tokens": int(i or 0), "output_tokens": int(o or 0), "cost_usd": float(Decimal(c or 0)), "calls": n}
        for src, i, o, c, n in usage_rows
    }
    return {
        "tasks": {"total": total, "by_status": by_status, "finished": finished},
        "rates": {
            "completion_rate": _rate(completed, finished),
            "failure_rate": _rate(failed, finished),
            "retry_rate_per_task": _rate(ev.get("retry.initiated", 0), total),
            "tool_error_rate": _rate(tool.get("FAILED", 0), tool_total),
            "tool_denial_rate": _rate(tool.get("DENIED", 0), tool_total),
            "checkpoint_recovery_success_rate": _rate(rec_ok, rec_done),
            "approval_frequency_per_task": _rate(ev.get("approval.requested", 0), total),
        },
        "events": ev,
        "tool_calls": {"total": tool_total, "by_status": tool},
        "recoveries": {"tasks_recovered": len(rec_tasks), "completed_after_recovery": rec_ok},
        "latency": {
            "avg_end_to_end_s": round(float(e2e), 3) if e2e is not None else None,
            "agents": [
                {"role": r, "count": n, "avg_ms": round(float(a), 1), "p95_ms": round(float(p), 1)}
                for r, n, a, p in agent_lat
            ],
        },
        "usage": usage,
        "usage_note": "provider_reported = token counts returned by the LLM API; estimated = computed "
        "locally (fake provider or missing usage). Cost is always an estimate from a pricing table.",
        "timeline": [{"hour": h.isoformat(), "status": st, "count": n} for h, st, n in recent],
    }


def _load_examples() -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    ex_dir = PROJECT_ROOT / "examples"
    if ex_dir.is_dir():
        for p in sorted(Path(ex_dir).glob("*.json")):
            try:
                examples.append({"name": p.stem, "request": json.loads(p.read_text(encoding="utf-8"))})
            except (OSError, ValueError):
                continue
    return examples


@meta.get("/meta")
async def get_meta(
    state: AppState = Depends(get_state), _: Principal = Depends(require_scopes("tasks:read"))
) -> dict[str, Any]:
    s = state.settings
    examples = await asyncio.to_thread(_load_examples)
    scenarios: list[dict[str, str]] = []
    if s.evaluation_mode:
        from app.evaluation.scenarios import SCENARIOS

        scenarios = [{"name": k, "description": v.description, "seed": v.seed_repo} for k, v in SCENARIOS.items()]
    return {
        "llm_provider": s.llm_provider.value,
        "llm_model": s.llm_model if s.llm_provider.value != "fake" else "fake-llm",
        "sandbox_backend": s.sandbox_backend.value,
        "evaluation_mode": s.evaluation_mode,
        "seeds": list_seeds(s.seed_repos_dir),
        "allowed_commands": {k: v.description for k, v in ALLOWLIST.items()},
        "scenarios": scenarios,
        "examples": examples,
        "budgets": {
            "max_graph_steps": s.max_graph_steps,
            "max_subtask_attempts": s.max_subtask_attempts,
            "task_deadline_s": s.task_deadline_s,
            "max_concurrent_agents": s.max_concurrent_agents,
        },
    }

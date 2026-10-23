"""LangGraph node implementations.

Conventions:
* Nodes read live resources from the RunContext (ContextVar) and return partial state updates.
* Expected failures (``AgentOSError``) are converted into ``state['error']`` so the supervisor
  routes them to the failure path with a recorded, classified error. Lease loss and simulated
  crashes propagate: the run stops without further writes and is recovered from the last checkpoint.
* Every status change goes through the validated state machine.
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select

from app.agents.auditor import review_changes, review_plan
from app.agents.coder import run_coder, run_single_agent
from app.agents.planner import run_planner
from app.agents.tester import run_tester
from app.domain.enums import ApprovalStatus, EventType
from app.domain.errors import (
    AgentOSError,
    FailureClass,
    LeaseLostError,
    TaskCancelledError,
)
from app.domain.states import TaskStatus, can_transition
from app.observability import metrics
from app.observability.tracing import start_span
from app.orchestration.context import RunContext, current
from app.orchestration.state import WorkflowState
from app.persistence.models import ApprovalRequest, ExecutionEvent, Plan, Subtask, SubtaskDependency, Task
from app.persistence.repository import (
    get_or_create_approval,
    record_event,
    save_artifact,
    transition_task,
    utcnow,
)
from app.schemas.agent import AgentResult
from app.schemas.plan import Plan as PlanModel
from app.supervisor.conflicts import detect_claim_conflicts
from app.supervisor.policy import Limits, decide
from app.tools.gateway import ApprovalRequiredError

NodeFn = Callable[[WorkflowState, RunContext], Awaitable[dict[str, Any]]]
PAUSE_CLASSES = {FailureClass.DEPENDENCY_UNAVAILABLE.value, FailureClass.TRANSIENT.value}


def _error_update(exc: AgentOSError, node: str) -> dict[str, Any]:
    rec = exc.to_record()
    # Dependencies that may come back (LLM provider down, ambiguous side effects) pause the task
    # for a human-triggered resume instead of failing it permanently.
    pause = rec["failure_class"] in PAUSE_CLASSES or rec["type"] == "ReconciliationRequiredError"
    return {"error": {**rec, "node": node, "pause": pause}}


def node(
    name: str, *, skip_on_error: bool = True
) -> Callable[[NodeFn], Callable[[WorkflowState], Awaitable[dict[str, Any]]]]:
    def deco(fn: NodeFn) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
        @functools.wraps(fn)
        async def wrapper(state: WorkflowState) -> dict[str, Any]:
            ctx = current()
            if skip_on_error and state.get("error"):
                return {}
            started = time.perf_counter()
            with start_span(
                f"node {name}",
                task_id=str(ctx.task_id),
                attributes={"graph.node": name, "graph.step": state.get("step_count", 0)},
            ):
                async with ctx.db.session() as s:
                    ctx.extra["cancel_requested"] = (
                        await s.scalar(select(Task.cancel_requested_at).where(Task.id == ctx.task_id))
                    ) is not None
                try:
                    updates = await fn(state, ctx) or {}
                except LeaseLostError:
                    raise
                except TaskCancelledError:
                    ctx.extra["cancel_requested"] = True
                    updates = {}
                except AgentOSError as exc:
                    updates = _error_update(exc, name)
                    async with ctx.write() as s:
                        await record_event(
                            s,
                            ctx.task_id,
                            EventType.ERROR,
                            run_id=ctx.run_id,
                            actor=name,
                            status="error",
                            message=f"{name} failed: {exc.message}",
                            data=updates["error"],
                        )
            metrics.NODE_LATENCY.labels(node=name).observe(time.perf_counter() - started)
            updates["step_count"] = int(state.get("step_count", 0)) + 1
            return updates

        return wrapper

    return deco


async def ensure_status(ctx: RunContext, target: TaskStatus, reason: str, actor: str = "supervisor", **kw: Any) -> None:
    async with ctx.write() as s:
        await transition_task(s, ctx.task_id, target, actor=actor, reason=reason, run_id=ctx.run_id, **kw)


def _limits(ctx: RunContext, mode: str) -> Limits:
    o, s = ctx.options, ctx.settings
    return Limits(
        supervised=mode == "supervised",
        security_review=True,
        loop_detection=bool(o.loop_detection),
        max_attempts=o.max_subtask_attempts or s.max_subtask_attempts,
        max_replans=s.max_replans,
        max_steps=o.max_graph_steps or s.max_graph_steps,
        deadline_s=s.task_deadline_s,
        max_tokens=o.max_total_tokens,
        require_plan_approval=bool(
            o.require_plan_approval if o.require_plan_approval is not None else s.require_plan_approval
        ),
    )


# --------------------------------------------------------------------------------- nodes


@node("validate_request")
async def validate_request(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    from app.security.commands import validate_command

    ctx.workspace.create(ctx.settings.seed_repos_dir, ctx.request.workspace_seed)
    for c in ctx.request.acceptance_criteria:  # defense in depth: re-check what the API accepted
        if c.kind == "tests_pass":
            validate_command(c.command, ctx.workspace.repo)
    await ensure_status(ctx, TaskStatus.PLANNING, "request validated; workspace prepared", actor="system")
    return {"phase": "new", "code_fingerprint": ctx.workspace.fingerprint()}


@node("planner")
async def planner(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    reason = state.get("replan_reason")
    await ensure_status(ctx, TaskStatus.PLANNING, f"replanning: {reason}" if reason else "planning")
    plan, _result, tokens = await run_planner(ctx, plan_version=state.get("plan_version", 0), replan_reason=reason)
    version = state.get("plan_version", 0) + 1
    async with ctx.write() as s:
        existing = await s.scalar(select(Plan).where(Plan.task_id == ctx.task_id, Plan.version == version))
        if existing is None:
            row = Plan(task_id=ctx.task_id, version=version, content=plan.model_dump(), reason=reason or "initial")
            s.add(row)
            await s.flush()
            ids: dict[str, Any] = {}
            for i, st in enumerate(plan.subtasks):
                sub = Subtask(
                    plan_id=row.id,
                    key=st.id,
                    order=i,
                    title=st.title,
                    kind=st.kind,
                    description=st.description,
                    target_files=st.target_files,
                    acceptance=st.acceptance,
                    required_permissions=st.required_permissions,
                )
                s.add(sub)
                await s.flush()
                ids[st.id] = sub.id
            for st in plan.subtasks:
                for dep in st.depends_on:
                    s.add(SubtaskDependency(subtask_id=ids[st.id], depends_on_id=ids[dep]))
            task = await s.get(Task, ctx.task_id)
            assert task is not None
            task.current_plan_version = version
            await record_event(
                s,
                ctx.task_id,
                EventType.PLAN_GENERATED,
                run_id=ctx.run_id,
                actor="planner",
                status="success",
                message=f"plan v{version}: {len(plan.subtasks)} subtasks - {plan.summary[:200]}",
                data={
                    "version": version,
                    "subtasks": [st.id for st in plan.subtasks],
                    "needs_clarification": plan.needs_clarification,
                },
            )
    subtasks = {
        st.id: {"status": "pending", "attempt": 1, "feedback": reason, "approval_id": None, "error": None}
        for st in plan.subtasks
    }
    return {
        "plan": plan.model_dump(),
        "plan_version": version,
        "subtasks": subtasks,
        "results": {},
        "phase": "planned",
        "plan_review": None,
        "plan_approved": False,
        "security": None,
        "security_approved": False,
        "test_report": None,
        "conflicts_accepted": False,
        "replan_reason": None,
        "tokens_used": state.get("tokens_used", 0) + tokens,
    }


@node("plan_policy_check")
async def plan_policy_check(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    if state.get("mode") != "supervised" or not state.get("plan"):
        return {}
    review, tokens = await review_plan(ctx, PlanModel.model_validate(state["plan"]), state.get("plan_version", 1))
    async with ctx.write() as s:
        await record_event(
            s,
            ctx.task_id,
            EventType.PLAN_POLICY_CHECKED,
            run_id=ctx.run_id,
            actor="security_auditor",
            status={"pass": "success", "escalate": "warning", "block": "error"}[review.verdict],
            message=f"plan policy check: {review.verdict} ({len(review.findings)} findings)",
            data={"verdict": review.verdict, "findings": [f.model_dump() for f in review.findings]},
        )
    return {"plan_review": review.model_dump(), "tokens_used": state.get("tokens_used", 0) + tokens}


@node("supervisor", skip_on_error=False)
async def supervisor(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    d = decide(
        dict(state),
        _limits(ctx, state.get("mode", "supervised")),
        now=time.time(),
        cancel_requested=bool(ctx.extra.get("cancel_requested")),
        workspace_fingerprint=ctx.workspace.fingerprint(),
    )
    async with ctx.write() as s:
        await record_event(
            s,
            ctx.task_id,
            EventType.SUPERVISOR_DECISION,
            run_id=ctx.run_id,
            actor="supervisor",
            message=f"-> {d.route}: {d.reason}",
            data={"route": d.route, "reason": d.reason, "step": state.get("step_count", 0)},
        )
        for etype, msg, data in d.events:
            await record_event(
                s, ctx.task_id, etype, run_id=ctx.run_id, actor="supervisor", status="warning", message=msg, data=data
            )
            if etype == "retry.initiated":
                metrics.RETRIES.labels(kind=str(data.get("kind", "subtask"))).inc()
            elif etype == "loop.detected":
                metrics.LOOPS_PREVENTED.labels(kind=str(data.get("kind", "loop"))).inc()
            elif etype == "conflict.detected":
                metrics.CONFLICTS.labels(kind=str(data.get("kind", "conflict"))).inc()
    return {**d.updates, "route": d.route, "route_reason": d.reason}


def route_from_supervisor(state: WorkflowState) -> str:
    return {
        "approval": "human_approval",
        "execute": "execute_subtasks",
        "review": "security_review",
        "test": "run_tests",
        "replan": "planner",
        "finalize": "finalize",
        "fail": "fail",
        "cancel": "cancel",
    }[state.get("route", "fail")]


@node("human_approval", skip_on_error=False)
async def human_approval(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    pa = dict(state.get("pending_approval") or {})
    if not pa:
        return {}
    async with ctx.write() as s:
        if pa.get("approval_id"):
            req = await s.get(ApprovalRequest, uuid.UUID(pa["approval_id"]))
        else:
            req, _ = await get_or_create_approval(
                s,
                task_id=ctx.task_id,
                run_id=ctx.run_id,
                kind=pa["kind"],
                reason=pa["reason"],
                payload=pa.get("payload", {}),
                requested_by_agent="supervisor",
                idempotency_key=f"{ctx.task_id}:{pa['key']}",
                ttl_seconds=ctx.settings.approval_ttl_s,
            )
            metrics.APPROVALS.labels(event="requested").inc()
        assert req is not None
        approval_id, status = str(req.id), req.status
        if status == ApprovalStatus.PENDING.value and req.expires_at < utcnow():
            req.status = status = ApprovalStatus.EXPIRED.value
        if status == ApprovalStatus.PENDING.value:
            await transition_task(
                s,
                ctx.task_id,
                TaskStatus.AWAITING_APPROVAL,
                actor="supervisor",
                reason=f"waiting for human approval: {pa['reason'][:300]}",
                run_id=ctx.run_id,
            )
    if status == ApprovalStatus.PENDING.value:
        # Pauses the graph; the checkpoint is persisted and the run ends. A decision via the API
        # enqueues a new run that resumes here. The decision itself is read from the database
        # (authoritative), not from the resume payload.
        interrupt({"approval_id": approval_id, "kind": pa["kind"], "reason": pa["reason"]})
        async with ctx.db.session() as s:
            req = await s.get(ApprovalRequest, uuid.UUID(approval_id))
            status = req.status if req else ApprovalStatus.EXPIRED.value
    approved = status == ApprovalStatus.APPROVED.value
    updates: dict[str, Any] = {
        "pending_approval": None,
        "approvals": {**state.get("approvals", {}), approval_id: status},
    }
    kind = pa["kind"]
    if kind == "tool":
        subs = dict(state.get("subtasks") or {})
        sid = pa.get("subtask")
        if sid in subs:
            st = dict(subs[sid])
            if approved:
                st.update(status="pending", approval_id=None)
                path = (req.payload or {}).get("args", {}).get("path") if req else None
                if path:
                    updates["approved_sensitive_paths"] = sorted({*state.get("approved_sensitive_paths", []), path})
            else:
                st.update(
                    status="failed",
                    error={"message": "a human rejected a sensitive operation", "failure_class": "policy_violation"},
                )
            subs[sid] = st
        updates["subtasks"] = subs
    elif kind == "plan":
        if approved:
            updates["plan_approved"] = True
        else:
            updates["error"] = {
                "message": f"plan rejected by human: {pa['reason'][:300]}",
                "failure_class": "policy_violation",
                "pause": False,
            }
    elif kind == "security_escalation":
        if approved:
            updates["security_approved"] = True
        else:
            updates["error"] = {
                "message": "security escalation rejected by human",
                "failure_class": "policy_violation",
                "pause": False,
            }
    elif kind == "conflict":
        if approved:
            updates["conflicts_accepted"] = True
        else:
            updates["error"] = {
                "message": "conflicting agent results rejected by human",
                "failure_class": "conflict",
                "pause": False,
            }
    resume = TaskStatus(pa.get("resume_status") or "RUNNING")
    async with ctx.write() as s:
        task = await s.get(Task, ctx.task_id)
        if (
            task
            and task.status == TaskStatus.AWAITING_APPROVAL.value
            and "error" not in updates
            and can_transition(TaskStatus.AWAITING_APPROVAL, resume)
        ):
            await transition_task(
                s, ctx.task_id, resume, actor="human", reason=f"approval {status.lower()}", run_id=ctx.run_id
            )
    return updates


@node("execute_subtasks")
async def execute_subtasks(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    await ensure_status(ctx, TaskStatus.RUNNING, "executing subtasks")
    if state.get("rollback_workspace"):
        await asyncio.to_thread(ctx.workspace.reset_to_base)
        async with ctx.write() as s:
            await record_event(
                s,
                ctx.task_id,
                EventType.ERROR,
                run_id=ctx.run_id,
                actor="supervisor",
                status="warning",
                message="workspace rolled back to baseline: blocked changes discarded before retry",
                data={"kind": "workspace_rollback"},
            )
    plan = state.get("plan") or {}
    subs = {k: dict(v) for k, v in (state.get("subtasks") or {}).items()}
    results = dict(state.get("results") or {})
    done = {sid for sid, st in subs.items() if st["status"] == "done"}
    ready = [
        st
        for st in plan.get("subtasks", [])
        if subs[st["id"]]["status"] == "pending" and set(st.get("depends_on", [])) <= done
    ]
    if not ready:
        blocked = [sid for sid, st in subs.items() if st["status"] == "pending"]
        return {
            "error": {"message": f"no runnable subtasks; blocked: {blocked}", "failure_class": "fatal", "pause": False}
        }
    sem = asyncio.Semaphore(max(1, ctx.settings.max_concurrent_agents))

    async def one(spec: dict[str, Any]) -> tuple[AgentResult, int]:
        async with sem:
            st = subs[spec["id"]]
            return await run_coder(
                ctx,
                subtask=spec,
                attempt=st["attempt"],
                plan_version=state.get("plan_version", 1),
                feedback=st.get("feedback"),
                plan_summary=plan.get("summary", ""),
            )

    for spec in ready:
        subs[spec["id"]]["status"] = "running"
    outcomes = await asyncio.gather(*(one(s) for s in ready), return_exceptions=True)
    tokens = 0
    for spec, out in zip(ready, outcomes, strict=True):
        sid = spec["id"]
        if isinstance(out, BaseException) and not isinstance(out, Exception):
            raise out  # simulated crash / cancellation: stop now, recover from checkpoint
        if isinstance(out, ApprovalRequiredError):
            subs[sid].update(status="awaiting_approval", approval_id=str(out.approval_id), approval_reason=out.reason)
        elif isinstance(out, (LeaseLostError,)):
            raise out
        elif isinstance(out, TaskCancelledError):
            subs[sid]["status"] = "pending"
            ctx.extra["cancel_requested"] = True
        elif isinstance(out, AgentOSError):
            subs[sid].update(status="failed", error=out.to_record())
        elif isinstance(out, Exception):
            raise out
        else:
            result, t = out
            tokens += t
            results[sid] = result.model_dump()
            if result.output.get("budget_exhausted"):
                subs[sid].update(
                    status="failed",
                    error={"message": "agent exhausted its tool-step budget", "failure_class": "budget_exceeded"},
                )
            else:
                subs[sid].update(status="done", error=None)
    return {
        "subtasks": subs,
        "results": results,
        "phase": "executing",
        "code_fingerprint": ctx.workspace.fingerprint(),
        "tokens_used": state.get("tokens_used", 0) + tokens,
        "rollback_workspace": False,
    }


@node("single_agent")
async def single_agent(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    await ensure_status(ctx, TaskStatus.RUNNING, "single agent executing the whole task")
    result, tokens = await run_single_agent(ctx)
    return {
        "results": {"single": result.model_dump()},
        "phase": "executed",
        "code_fingerprint": ctx.workspace.fingerprint(),
        "tokens_used": state.get("tokens_used", 0) + tokens,
    }


async def _injection_flags(ctx: RunContext) -> list[str]:
    async with ctx.db.session() as s:
        rows = (
            await s.scalars(
                select(ExecutionEvent.data).where(
                    ExecutionEvent.task_id == ctx.task_id, ExecutionEvent.type == EventType.ERROR.value
                )
            )
        ).all()
    return sorted({f for d in rows if d.get("kind") == "prompt_injection_suspected" for f in d.get("flags", [])})


@node("security_review")
async def security_review(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    await ensure_status(ctx, TaskStatus.VALIDATING, "security review of changes")
    attempt = max([st["attempt"] for st in (state.get("subtasks") or {}).values()] or [1])
    review, tokens = await review_changes(
        ctx,
        state.get("plan") or {},
        attempt,
        state.get("plan_version", 1),
        set(state.get("approved_sensitive_paths", [])),
        await _injection_flags(ctx),
    )
    async with ctx.write() as s:
        await record_event(
            s,
            ctx.task_id,
            EventType.SECURITY_REVIEWED,
            run_id=ctx.run_id,
            actor="security_auditor",
            status={"pass": "success", "escalate": "warning", "block": "error"}[review.verdict],
            message=f"security review: {review.verdict} ({len(review.findings)} findings)",
            data={"verdict": review.verdict, "findings": [f.model_dump() for f in review.findings]},
        )
    if review.verdict == "block":
        metrics.SECURITY_DENIALS.labels(rule="security_review_block").inc()
    return {"security": review.model_dump(), "phase": "reviewed", "tokens_used": state.get("tokens_used", 0) + tokens}


@node("run_tests")
async def run_tests(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    await ensure_status(ctx, TaskStatus.VALIDATING, "running acceptance tests")
    runs = int(state.get("test_runs", 0)) + 1
    report, _res, tokens = await run_tester(
        ctx, attempt=runs, plan_version=state.get("plan_version", 0), analyze=state.get("mode") != "single_agent"
    )
    passed = sum(c.passed for c in report.criteria)
    claims = detect_claim_conflicts(state.get("results") or {}, report.model_dump())
    async with ctx.write() as s:
        await record_event(
            s,
            ctx.task_id,
            EventType.TEST_EXECUTED,
            run_id=ctx.run_id,
            actor="tester",
            status="success" if report.outcome == "passed" else "error",
            message=f"acceptance tests {report.outcome}: {passed}/{len(report.criteria)} criteria passed",
            data={
                "outcome": report.outcome,
                "criteria": [c.model_dump() for c in report.criteria],
                "failure_signature": report.failure_signature,
                "flaky": report.flaky,
                "fingerprint": report.diff_fingerprint,
            },
        )
        for c in claims:
            await record_event(
                s,
                ctx.task_id,
                EventType.CONFLICT_DETECTED,
                run_id=ctx.run_id,
                actor="supervisor",
                status="warning",
                message=c["detail"],
                data=c,
            )
            metrics.CONFLICTS.labels(kind=c["kind"]).inc()
    history = [
        *state.get("history", []),
        {
            "fingerprint": report.diff_fingerprint,
            "signature": report.failure_signature,
            "outcome": report.outcome,
            "run": runs,
        },
    ]
    return {
        "test_report": report.model_dump(),
        "phase": "tested",
        "history": history,
        "test_runs": runs,
        "tokens_used": state.get("tokens_used", 0) + tokens,
    }


def route_after_single_tests(state: WorkflowState) -> str:
    if state.get("error"):
        return "fail"
    rep = state.get("test_report") or {}
    return "finalize" if rep.get("outcome") == "passed" else "fail"


def route_after_single_agent(state: WorkflowState) -> str:
    return "fail" if state.get("error") else "run_tests"


@node("finalize")
async def finalize(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    rep = state.get("test_report") or {}
    fp = ctx.workspace.fingerprint()
    required_ok = rep.get("outcome") == "passed" and all(
        c["passed"] for c in rep.get("criteria", []) if c.get("required", True)
    )
    if not required_ok or rep.get("diff_fingerprint") != fp:
        return {
            "error": {
                "message": "refusing to complete: acceptance evidence missing or stale",
                "failure_class": "fatal",
                "pause": False,
            }
        }
    diff = ctx.workspace.diff()
    changed = ctx.workspace.changed_files()
    arts: dict[str, str] = {}
    async with ctx.write() as s:
        for kind, name, content, ctype in (
            ("diff", "changes.patch", diff or "(no changes)\n", "text/x-diff"),
            ("test_report", "test_report.json", _json(rep), "application/json"),
            ("plan", "plan.json", _json(state.get("plan") or {}), "application/json"),
            ("summary", "summary.md", _summary_md(state, changed), "text/markdown"),
        ):
            aid, _ = await save_artifact(
                s,
                task_id=ctx.task_id,
                run_id=ctx.run_id,
                kind=kind,
                name=name,
                content=content,
                content_type=ctype,
                artifact_root=ctx.settings.artifact_root,
                idempotency_key=f"{ctx.task_id}:final:{kind}:{fp}",
                meta={"fingerprint": fp},
            )
            arts[kind] = str(aid)
    # Crash point: artifacts are stored (idempotently) but the task is not yet COMPLETED.
    await ctx.faults.crash_point("during_finalize")
    result = {
        "summary": f"All {len(rep.get('criteria', []))} acceptance criteria passed. Files changed: {', '.join(p for p, _ in changed) or 'none'}.",
        "artifacts": arts,
        "criteria": rep.get("criteria", []),
        "files_changed": [{"path": p, "status": st} for p, st in changed],
        "fingerprint": fp,
    }
    async with ctx.write() as s:
        await transition_task(
            s,
            ctx.task_id,
            TaskStatus.COMPLETED,
            actor="supervisor",
            reason="all required acceptance criteria passed",
            run_id=ctx.run_id,
            criteria_passed=True,
            result=result,
        )
        task = await s.get(Task, ctx.task_id)
        if task is not None:
            metrics.TASK_E2E_LATENCY.labels(status="COMPLETED").observe((utcnow() - task.created_at).total_seconds())
    metrics.TASKS_FINISHED.labels(status="COMPLETED").inc()
    return {"final": result}


@node("fail", skip_on_error=False)
async def fail(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    err = dict(
        state.get("error") or {"message": state.get("route_reason", "workflow failed"), "failure_class": "fatal"}
    )
    async with ctx.write() as s:
        task = await s.get(Task, ctx.task_id)
        assert task is not None
        current_status = TaskStatus(task.status)
        target = TaskStatus.RECOVERY_PENDING if err.get("pause") else TaskStatus.FAILED
        if not can_transition(current_status, target):
            target = TaskStatus.FAILED
        report = {
            "error": err,
            "test_report": state.get("test_report"),
            "security": state.get("security"),
            "conflicts": state.get("conflicts", []),
            "history": state.get("history", []),
        }
        await save_artifact(
            s,
            task_id=ctx.task_id,
            run_id=ctx.run_id,
            kind="failure_report",
            name="failure.json",
            content=_json(report),
            content_type="application/json",
            artifact_root=ctx.settings.artifact_root,
            idempotency_key=f"{ctx.task_id}:failure:{state.get('step_count', 0)}",
        )
        diff = ctx.workspace.diff() if ctx.workspace.exists() else ""
        if diff:
            await save_artifact(
                s,
                task_id=ctx.task_id,
                run_id=ctx.run_id,
                kind="diff",
                name="partial_changes.patch",
                content=diff,
                content_type="text/x-diff",
                artifact_root=ctx.settings.artifact_root,
                idempotency_key=f"{ctx.task_id}:failure-diff:{state.get('step_count', 0)}",
            )
        if current_status != target:
            await transition_task(
                s,
                ctx.task_id,
                target,
                actor="supervisor",
                reason=str(err.get("message"))[:1000],
                run_id=ctx.run_id,
                error=err,
            )
        if target == TaskStatus.FAILED:
            metrics.TASK_E2E_LATENCY.labels(status="FAILED").observe((utcnow() - task.created_at).total_seconds())
    metrics.TASKS_FINISHED.labels(status=target.value).inc()
    return {"final": {"status": target.value, "error": err}}


@node("cancel", skip_on_error=False)
async def cancel(state: WorkflowState, ctx: RunContext) -> dict[str, Any]:
    async with ctx.write() as s:
        await transition_task(
            s, ctx.task_id, TaskStatus.CANCELLED, actor="user", reason="cancelled at a safe point", run_id=ctx.run_id
        )
        for req in (
            await s.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.task_id == ctx.task_id, ApprovalRequest.status == ApprovalStatus.PENDING.value
                )
            )
        ).all():
            req.status = ApprovalStatus.EXPIRED.value
    metrics.TASKS_FINISHED.labels(status="CANCELLED").inc()
    return {"final": {"status": "CANCELLED"}}


def _json(data: Any) -> str:
    import json

    return json.dumps(data, indent=2, sort_keys=True, default=str)


def _summary_md(state: WorkflowState, changed: list[tuple[str, str]]) -> str:
    rep = state.get("test_report") or {}
    lines = [
        "# Task summary",
        "",
        f"Plan version: {state.get('plan_version')}",
        f"Retries: {state.get('retries', 0)}",
        f"Replans: {state.get('replans', 0)}",
        "",
        "## Acceptance criteria",
        "",
    ]
    lines += [
        f"- {'PASS' if c['passed'] else 'FAIL'} `{c['criterion_id']}`: {c.get('detail', '')}"
        for c in rep.get("criteria", [])
    ]
    lines += ["", "## Files changed", ""] + [f"- {st}: `{p}`" for p, st in changed]
    for sid, r in (state.get("results") or {}).items():
        lines += ["", f"### {sid}", "", str(r.get("summary", ""))]
    return "\n".join(lines) + "\n"

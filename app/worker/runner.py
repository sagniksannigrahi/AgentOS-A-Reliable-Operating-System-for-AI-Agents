"""Executes one claimed task run: builds the run context, resumes or starts the LangGraph workflow
from its PostgreSQL checkpoint, records checkpoint references, and keeps the lease alive.

Crash semantics (see docs/RECOVERY.md):
* Checkpoints are written synchronously after every node (``durability="sync"``), so a resumed
  run continues *after* the last completed node.
* A node interrupted mid-way is re-executed from its start. Its LLM calls are repeated (and paid
  for again); its tool calls are deduplicated or reconciled by idempotency key in the gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from opentelemetry.trace import SpanKind
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError

from app.config.settings import LLMProviderName, Settings
from app.domain.enums import ApprovalStatus, EventType, RunReason, RunStatus
from app.domain.errors import LeaseLostError, LLMUnavailableError, SimulatedCrash, error_record
from app.domain.states import TERMINAL_STATES, TaskStatus, can_transition
from app.llm.base import LLMProvider
from app.observability import metrics
from app.observability.logging import bind_context, get_logger, unbind_context
from app.observability.tracing import context_from_traceparent, start_span
from app.orchestration.context import CURRENT_RUN, RunContext
from app.orchestration.faults import FaultInjector
from app.orchestration.graph import build_graph
from app.orchestration.state import STATE_SCHEMA_VERSION, initial_state
from app.persistence.db import Database
from app.persistence.models import ApprovalRequest, Task
from app.persistence.redis_client import RedisCoordinator
from app.persistence.repository import (
    ClaimedRun,
    abandon_inflight_tool_calls,
    active_run,
    count_recoveries,
    enqueue_run,
    finish_run,
    heartbeat,
    record_checkpoint,
    record_event,
    release_lease,
    transition_task,
)
from app.schemas.api import TaskCreate
from app.tools.builtin import ToolEnv, build_registry
from app.tools.gateway import ToolGateway
from app.tools.sandbox import Sandbox
from app.tools.workspace import Workspace

log = get_logger(__name__)
MODES = ("supervised", "unsupervised", "single_agent")


class WorkflowRunner:
    def __init__(
        self, settings: Settings, db: Database, checkpointer: Any, redis: RedisCoordinator, worker_id: str
    ) -> None:
        self.settings = settings
        self.db = db
        self.redis = redis
        self.worker_id = worker_id
        self.registry = build_registry(settings)
        self.sandbox = Sandbox(settings)
        self.graphs = {m: build_graph(m, checkpointer) for m in MODES}
        self._anthropic: LLMProvider | None = None

    def _llm(self, request: dict[str, Any]) -> LLMProvider:
        if self.settings.llm_provider == LLMProviderName.FAKE:
            from app.llm.fake import FakeLLM

            return FakeLLM.for_task(request)
        if self._anthropic is None:
            from app.llm.anthropic_provider import AnthropicProvider

            self._anthropic = AnthropicProvider(self.settings)
        return self._anthropic

    # --------------------------------------------------------------------------- lifecycle

    async def execute(self, claimed: ClaimedRun) -> None:
        bind_context(task_id=str(claimed.task_id), run_id=str(claimed.run_id), worker_id=self.worker_id)
        metrics.RUNS_STARTED.labels(reason=claimed.reason).inc()
        metrics.ACTIVE_RUNS.inc()
        main = asyncio.current_task()
        lost = asyncio.Event()
        hb = asyncio.create_task(self._heartbeat(claimed, main, lost))
        try:
            with start_span(
                "task run",
                task_id=str(claimed.task_id),
                kind=SpanKind.CONSUMER,
                context=context_from_traceparent(claimed.traceparent),
                attributes={"run.attempt": claimed.attempt, "run.reason": claimed.reason, "worker.id": self.worker_id},
            ):
                await self._execute(claimed)
        except SimulatedCrash:
            raise  # emulate process death: no cleanup, the lease is left to expire
        except asyncio.CancelledError:
            if lost.is_set():
                log.warning("run cancelled after losing its lease")
                return
            with contextlib.suppress(Exception):
                async with self.db.session() as s:
                    await release_lease(s, claimed.run_id, claimed.fencing_token)
            raise
        except LeaseLostError:
            log.warning("lease lost; stopping run without further writes")
        finally:
            hb.cancel()
            with contextlib.suppress(BaseException):
                await hb
            metrics.ACTIVE_RUNS.dec()
            unbind_context("task_id", "run_id")

    async def _heartbeat(self, claimed: ClaimedRun, main: asyncio.Task[Any] | None, lost: asyncio.Event) -> None:
        interval = self.settings.heartbeat_interval_s
        failures = 0
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.db.session() as s:
                    ok = await heartbeat(s, claimed.run_id, claimed.fencing_token, self.settings.lease_seconds)
                failures = 0
            except (OperationalError, DBAPIError, OSError) as exc:
                failures += 1
                log.warning("heartbeat failed", error=str(exc)[:200], failures=failures)
                ok = failures * interval < self.settings.lease_seconds
            if not ok:
                lost.set()
                log.error("lease lost; cancelling run")
                if main is not None:
                    main.cancel()
                return

    async def _finish(
        self, claimed: ClaimedRun, status: RunStatus, message: str, error: dict[str, Any] | None = None
    ) -> None:
        async with self.db.session() as s:
            if await finish_run(s, claimed.run_id, claimed.fencing_token, status, error):
                await record_event(
                    s,
                    claimed.task_id,
                    EventType.RUN_FINISHED,
                    run_id=claimed.run_id,
                    status="error" if status == RunStatus.FAILED else "info",
                    message=f"run #{claimed.attempt} {status.value.lower()}: {message}",
                    data={"status": status.value},
                )

    async def _terminate(self, claimed: ClaimedRun, target: TaskStatus, reason: str, error: dict[str, Any]) -> None:
        async with self.db.session() as s:
            task = await s.get(Task, claimed.task_id)
            if task is None or TaskStatus(task.status) in TERMINAL_STATES:
                return
            cur = TaskStatus(task.status)
            if cur != target:
                if not can_transition(cur, target):
                    target = TaskStatus.FAILED
                await transition_task(
                    s, claimed.task_id, target, actor="worker", reason=reason, run_id=claimed.run_id, error=error
                )
        metrics.TASKS_FINISHED.labels(status=target.value).inc()

    # ----------------------------------------------------------------------------- execute

    async def _execute(self, claimed: ClaimedRun) -> None:
        s_ = self.settings
        async with self.db.session() as s:
            task = await s.get(Task, claimed.task_id)
            assert task is not None
            await record_event(
                s,
                task.id,
                EventType.RUN_STARTED,
                run_id=claimed.run_id,
                actor=f"worker:{self.worker_id}",
                message=f"run #{claimed.attempt} started ({claimed.reason}) on {self.worker_id}",
                data={"attempt": claimed.attempt, "reason": claimed.reason, "worker": self.worker_id},
            )
        if TaskStatus(task.status) in TERMINAL_STATES:
            await self._finish(claimed, RunStatus.SUCCEEDED, f"task already {task.status}")
            return
        request = TaskCreate.model_validate(task.request)
        opts = request.options
        mode = opts.workflow_mode

        if claimed.reason == RunReason.RECOVERY.value:
            if not opts.recovery:
                await self._terminate(
                    claimed,
                    TaskStatus.FAILED,
                    "worker crashed and checkpoint recovery is disabled",
                    {"message": "worker crashed; recovery disabled", "failure_class": "infrastructure"},
                )
                metrics.RECOVERIES.labels(outcome="disabled").inc()
                await self._finish(claimed, RunStatus.SUCCEEDED, "recovery disabled")
                return
            async with self.db.session() as s:
                recoveries = await count_recoveries(s, task.id)
            if recoveries > s_.max_recoveries:
                await self._terminate(
                    claimed,
                    TaskStatus.RECOVERY_PENDING,
                    f"worker crashed {recoveries} times; waiting for a human to resume",
                    {"message": "repeated worker crashes", "failure_class": "infrastructure", "recoveries": recoveries},
                )
                metrics.RECOVERIES.labels(outcome="gave_up").inc()
                await self._finish(claimed, RunStatus.SUCCEEDED, "too many recoveries")
                return
            async with self.db.session() as s:
                abandoned = await abandon_inflight_tool_calls(s, task.id, claimed.run_id)
                await record_event(
                    s,
                    task.id,
                    EventType.RECOVERY_STARTED,
                    run_id=claimed.run_id,
                    actor=f"worker:{self.worker_id}",
                    status="warning",
                    message=f"recovering after worker failure (from run {claimed.recovered_from_run_id}); "
                    f"{abandoned} in-flight tool call(s) marked for reconciliation",
                    data={"recovered_from": str(claimed.recovered_from_run_id), "abandoned_tool_calls": abandoned},
                )

        faults = (
            FaultInjector(task.id, tuple(opts.faults), self.db)
            if s_.evaluation_mode and opts.faults
            else FaultInjector.disabled()
        )
        workspace = Workspace.for_task(s_.workspace_root, str(task.id))
        await asyncio.to_thread(workspace.create, s_.seed_repos_dir, request.workspace_seed)
        env = ToolEnv(settings=s_, workspace=workspace, sandbox=self.sandbox, db=self.db, task_id=task.id)
        gateway = ToolGateway(
            self.registry,
            env,
            self.db,
            faults,
            max_identical_calls=s_.max_identical_tool_calls,
            output_limit=s_.tool_output_limit_bytes,
            approval_ttl_s=s_.approval_ttl_s,
            loop_detection=opts.loop_detection,
        )
        try:
            llm = self._llm(task.request)
        except LLMUnavailableError as exc:
            await self._terminate(claimed, TaskStatus.RECOVERY_PENDING, exc.message, exc.to_record())
            await self._finish(claimed, RunStatus.FAILED, exc.message, exc.to_record())
            return
        ctx = RunContext(
            settings=s_,
            db=self.db,
            llm=llm,
            gateway=gateway,
            workspace=workspace,
            faults=faults,
            task_id=task.id,
            run_id=claimed.run_id,
            fencing_token=claimed.fencing_token,
            request=request,
        )
        token = CURRENT_RUN.set(ctx)
        try:
            await self._run_graph(claimed, task, ctx, mode)
        except (SimulatedCrash, asyncio.CancelledError, LeaseLostError):
            raise
        except GraphRecursionError:
            err = {"message": "graph recursion limit reached", "failure_class": "budget_exceeded"}
            await self._terminate(claimed, TaskStatus.FAILED, err["message"], err)
            await self._finish(claimed, RunStatus.FAILED, err["message"], err)
        except (OperationalError, DBAPIError) as exc:
            log.error(
                "database error during run; lease will expire and the run will be recovered", error=str(exc)[:300]
            )
            raise
        except Exception as exc:
            rec = error_record(exc)
            log.exception("run failed with an unexpected error")
            with contextlib.suppress(Exception):
                await self._terminate(claimed, TaskStatus.FAILED, f"unexpected error: {rec['message'][:300]}", rec)
                await self._finish(claimed, RunStatus.FAILED, rec["message"][:200], rec)
        finally:
            CURRENT_RUN.reset(token)

    async def _run_graph(self, claimed: ClaimedRun, task: Task, ctx: RunContext, mode: str) -> None:
        graph = self.graphs[mode]
        thread_id = f"task-{task.id}"
        max_steps = ctx.options.max_graph_steps or self.settings.max_graph_steps
        cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}, "recursion_limit": max_steps * 3 + 25}
        snapshot = await graph.aget_state(cfg)
        inp: Any
        if snapshot.values:
            if snapshot.values.get("schema_version") != STATE_SCHEMA_VERSION:
                err = {
                    "message": f"checkpoint schema v{snapshot.values.get('schema_version')} != v{STATE_SCHEMA_VERSION}",
                    "failure_class": "fatal",
                }
                await self._terminate(claimed, TaskStatus.RECOVERY_PENDING, err["message"], err)
                await self._finish(claimed, RunStatus.FAILED, err["message"], err)
                return
            interrupted = any(t.interrupts for t in snapshot.tasks)
            if claimed.reason == RunReason.RESUME.value:
                if not snapshot.next:
                    # The workflow parked itself through the failure path (e.g. LLM or test
                    # infrastructure unavailable). Rewind with the error cleared: back to the
                    # planner if no plan exists yet, otherwise to the supervisor to re-decide.
                    phase = snapshot.values.get("phase")
                    if mode == "single_agent" or phase in ("new", "replanning") or not snapshot.values.get("plan"):
                        as_node = "validate_request"
                    else:
                        as_node = "plan_policy_check"
                    await graph.aupdate_state(
                        cfg, {"error": None, "infra_retries": 0, "flaky_reruns": 0, "final": None}, as_node=as_node
                    )
                    snapshot = await graph.aget_state(cfg)
                async with self.db.session() as s:
                    t = await s.get(Task, task.id)
                    if t and t.status in (TaskStatus.RECOVERY_PENDING.value, TaskStatus.PAUSED.value) and t.paused_from:
                        target = TaskStatus(t.paused_from)
                        if can_transition(TaskStatus(t.status), target):
                            await transition_task(
                                s, task.id, target, actor="human", reason="resumed", run_id=claimed.run_id
                            )
            if interrupted:
                decided = await self._pending_decision(task.id)
                if claimed.reason != RunReason.APPROVAL.value and not decided:
                    await self._finish(claimed, RunStatus.PAUSED, "still waiting for a human decision")
                    return
                inp = Command(resume=claimed.resume_payload or {"decided": True})
            elif not snapshot.next:
                err = {"message": "workflow already finished but task is not terminal", "failure_class": "fatal"}
                await self._terminate(claimed, TaskStatus.RECOVERY_PENDING, err["message"], err)
                await self._finish(claimed, RunStatus.FAILED, err["message"], err)
                return
            else:
                inp = None
            if claimed.reason in (RunReason.RECOVERY.value, RunReason.RESUME.value):
                ck = snapshot.config["configurable"].get("checkpoint_id")
                async with ctx.write() as s:
                    await record_checkpoint(
                        s,
                        task_id=task.id,
                        run_id=claimed.run_id,
                        thread_id=thread_id,
                        checkpoint_id=ck,
                        node=None,
                        step=snapshot.metadata.get("step") if snapshot.metadata else None,
                        restored=True,
                    )
                    await record_event(
                        s,
                        task.id,
                        EventType.CHECKPOINT_RESTORED,
                        run_id=claimed.run_id,
                        actor=f"worker:{self.worker_id}",
                        status="success",
                        message=f"resuming from checkpoint {ck}; next: {list(snapshot.next)}",
                        data={
                            "checkpoint_id": ck,
                            "next": list(snapshot.next),
                            "step_count": snapshot.values.get("step_count"),
                        },
                    )
        else:
            inp = initial_state(str(task.id), mode)
            if claimed.reason == RunReason.RECOVERY.value:
                async with ctx.write() as s:
                    await record_event(
                        s,
                        task.id,
                        EventType.CHECKPOINT_RESTORED,
                        run_id=claimed.run_id,
                        status="warning",
                        message="no checkpoint existed yet; starting the workflow from the beginning (nothing was persisted)",
                        data={"checkpoint_id": None},
                    )

        async for chunk in graph.astream(inp, cfg, stream_mode="updates", durability="sync"):
            for node_name in chunk:
                if node_name == "__interrupt__":
                    continue
                snap = await graph.aget_state(cfg)
                ck = snap.config["configurable"].get("checkpoint_id")
                async with ctx.write() as s:
                    created = await record_checkpoint(
                        s,
                        task_id=task.id,
                        run_id=claimed.run_id,
                        thread_id=thread_id,
                        checkpoint_id=ck,
                        node=node_name,
                        step=snap.values.get("step_count"),
                    )
                    if created:
                        await record_event(
                            s,
                            task.id,
                            EventType.CHECKPOINT_WRITTEN,
                            run_id=claimed.run_id,
                            actor="checkpointer",
                            message=f"checkpoint after {node_name}",
                            data={"checkpoint_id": ck, "node": node_name},
                        )
                metrics.CHECKPOINTS_WRITTEN.inc()
                await ctx.faults.crash_point("after_node", node_name)

        final = await graph.aget_state(cfg)
        if claimed.reason == RunReason.RECOVERY.value:
            metrics.RECOVERIES.labels(outcome="resumed").inc()
        if final.next:
            await self._finish(claimed, RunStatus.PAUSED, "waiting for human approval")
            if await self._pending_decision(task.id):
                await self._enqueue_continuation(task.id)
            return
        await self._finish(claimed, RunStatus.SUCCEEDED, "workflow reached END")

    async def _pending_decision(self, task_id: uuid.UUID) -> bool:
        """True if the approval the workflow is waiting on has already been decided."""
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.task_id == task_id)
                    .order_by(ApprovalRequest.created_at.desc())
                    .limit(1)
                )
            ).all()
        return bool(rows) and rows[0].status != ApprovalStatus.PENDING.value

    async def _enqueue_continuation(self, task_id: uuid.UUID) -> None:
        async with self.db.session() as s:
            task = await s.get(Task, task_id)
            if task is not None and await active_run(s, task_id) is None:
                await enqueue_run(s, task, RunReason.APPROVAL, resume_payload={"decided": True})
        await self.redis.notify_work()

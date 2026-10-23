"""Reproducible benchmark harness.

Runs every (scenario x configuration x trial) through the *real* stack — API service layer,
PostgreSQL queue, worker, LangGraph, gateway, sandbox — with the deterministic fake LLM. Trial
``i`` uses seed ``i`` so results are exactly reproducible.

What is measured vs simulated
-----------------------------
* MEASURED on the machine running the harness: wall-clock latency, test pass rates (the harness
  re-runs the acceptance criteria itself on the final workspace), tool failures/denials,
  recoveries, loop detections, policy violations found in delivered code.
* SIMULATED: agent behaviour (scripted scenarios), token counts (estimated ~4 chars/token) and
  cost (notional fake-llm price). These say nothing about any real model.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.agents.auditor import change_findings
from app.api.deps import AppState, Principal
from app.api.services import create_task, decide_approval
from app.config.settings import Settings
from app.domain.errors import SimulatedCrash
from app.domain.states import TERMINAL_STATES, TaskStatus
from app.evaluation.scenarios import SCENARIOS, Scenario
from app.evaluation.stats import bootstrap_mean, percentile, wilson
from app.llm.fake import FakeLLM
from app.persistence.db import Database
from app.persistence.models import ApprovalRequest, ExecutionEvent, LLMUsage, Task, TaskRun, ToolInvocation
from app.persistence.redis_client import RedisCoordinator
from app.persistence.repository import create_api_key
from app.schemas.api import TaskCreate
from app.security.commands import validate_command
from app.tools.sandbox import Sandbox, classify_pytest
from app.tools.workspace import Workspace

CONFIGS: dict[str, dict[str, Any]] = {
    "single_agent": {"workflow_mode": "single_agent", "loop_detection": False, "recovery": False},
    "multi_agent_unsupervised": {"workflow_mode": "unsupervised", "loop_detection": False, "recovery": False},
    "supervised": {"workflow_mode": "supervised", "loop_detection": False, "recovery": False},
    "supervised_recovery_loopdetect": {"workflow_mode": "supervised", "loop_detection": True, "recovery": True},
}


@dataclass
class TrialResult:
    scenario: str
    config: str
    seed: int
    expected: str
    status: str
    completed: bool
    correct: bool
    criteria_pass_rate: float
    escalated: bool
    crashed: bool
    recovered_ok: bool
    loop_detected: bool
    loop_ground_truth: bool
    duplicate_action_blocks: int
    duplicate_side_effects: int
    duplicates_prevented: int
    latency_s: float
    tokens: int
    cost_usd: float
    tool_failures: int
    tool_denials: int
    policy_violation: bool
    retries: int
    notes: list[str] = field(default_factory=list)


def _loop_ground_truth(sc: Scenario, seed: int, max_attempts: int) -> bool:
    """A run is a *true* loop if no attempt within the budget could ever succeed."""
    if sc.expected == "escalate":
        return False  # the correct behaviour stops before any coding happens
    if sc.bug_always:
        return True
    if sc.bug_probability > 0:
        fake = FakeLLM(sc, trial_seed=seed)
        return all(fake._variant(sc, "impl", a, 1)[1] == "buggy" for a in range(1, max_attempts + 1))
    return False


async def _evaluate_workspace(settings: Settings, ws: Workspace, request: TaskCreate) -> float:
    """Independently re-check acceptance criteria on the final workspace."""
    if not ws.exists():
        return 0.0
    passed = 0
    sandbox = Sandbox(settings)
    for c in request.acceptance_criteria:
        if c.kind == "tests_pass":
            res = await sandbox.run(validate_command(c.command, ws.repo), ws.repo.resolve())
            passed += classify_pytest(res) == "passed"
        else:
            p = ws.repo / (c.path or "")
            text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
            ok = (
                p.is_file()
                if c.kind == "file_exists"
                else text is not None and ((c.text in text) if c.kind == "file_contains" else (c.text not in text))
            )
            passed += bool(ok)
    return passed / len(request.acceptance_criteria)


class Harness:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings)
        self.state = AppState(settings=settings, db=self.db, redis=RedisCoordinator(None))
        self.worker: Any = None
        self.operator: Principal | None = None
        self.approver: Principal | None = None

    async def __aenter__(self) -> Harness:
        from app.worker.main import Worker

        async with self.db.session() as s:
            op, _ = await create_api_key(s, f"bench-operator-{uuid.uuid4().hex[:6]}", ["tasks:read", "tasks:write"])
            ap, _ = await create_api_key(
                s, f"bench-approver-{uuid.uuid4().hex[:6]}", ["approvals:decide", "approvals:read"]
            )
        self.operator = Principal(op.id, op.name, frozenset(op.scopes))
        self.approver = Principal(ap.id, ap.name, frozenset(ap.scopes))
        self.worker = Worker(self.settings, worker_id="bench-worker")
        await self.worker.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.worker.close()
        await self.db.dispose()

    async def _drain(self) -> bool:
        crashed = False
        for _ in range(10):
            try:
                while await self.worker.run_once():
                    pass
                return crashed
            except SimulatedCrash:
                crashed = True
                async with self.db.session() as s:
                    from sqlalchemy import update

                    from app.persistence.repository import utcnow

                    await s.execute(
                        update(TaskRun).where(TaskRun.status == "RUNNING").values(lease_expires_at=utcnow())
                    )
        return crashed

    async def run_trial(self, sc: Scenario, config: str, seed: int) -> TrialResult:
        assert self.operator and self.approver
        body = sc.task_request(**CONFIGS[config], seed=seed)
        payload = TaskCreate.model_validate(body)
        started = time.perf_counter()
        task, _ = await create_task(self.state, payload, self.operator, None)
        crashed = await self._drain()
        escalated = False
        for _ in range(5):  # play the human: decide pending approvals per the scenario's policy
            async with self.db.session() as s:
                pending = (
                    await s.scalars(
                        select(ApprovalRequest).where(
                            ApprovalRequest.task_id == task.id, ApprovalRequest.status == "PENDING"
                        )
                    )
                ).all()
            if not pending:
                break
            for req in pending:
                escalated = True
                await decide_approval(self.state, req.id, self.approver, sc.approval_policy, "benchmark auto-decision")
            crashed |= await self._drain()
        latency = time.perf_counter() - started
        return await self._collect(sc, config, seed, task.id, payload, latency, crashed, escalated)

    async def _collect(
        self,
        sc: Scenario,
        config: str,
        seed: int,
        task_id: uuid.UUID,
        payload: TaskCreate,
        latency: float,
        crashed: bool,
        escalated: bool,
    ) -> TrialResult:
        async with self.db.session() as s:
            task = await s.get(Task, task_id)
            assert task is not None
            events = (await s.scalars(select(ExecutionEvent).where(ExecutionEvent.task_id == task_id))).all()
            tools = (await s.scalars(select(ToolInvocation).where(ToolInvocation.task_id == task_id))).all()
            usage = (
                await s.execute(
                    select(
                        func.coalesce(func.sum(LLMUsage.input_tokens + LLMUsage.output_tokens), 0),
                        func.coalesce(func.sum(LLMUsage.cost_usd), 0),
                    ).where(LLMUsage.task_id == task_id)
                )
            ).one()
            runs = (await s.scalars(select(TaskRun).where(TaskRun.task_id == task_id))).all()
        status = task.status
        completed = status == TaskStatus.COMPLETED.value
        ws = Workspace.for_task(self.settings.workspace_root, str(task_id))
        pass_rate = await _evaluate_workspace(self.settings, ws, payload)
        violation = False
        if completed and ws.exists():
            findings = change_findings(ws.added_lines(), ws.changed_files(), set(), set())
            violation = any(f.severity in ("critical", "high") for f in findings)
        no_progress = any(e.type == "loop.detected" and e.data.get("kind") == "no_progress" for e in events)
        dup_blocks = sum(1 for e in events if e.type == "loop.detected" and e.data.get("kind") == "duplicate_action")
        # Physically repeated side effects: the same write (path+content) completed more than once
        # within one agent attempt.
        seen: dict[tuple[str, str], int] = defaultdict(int)
        for t in tools:
            if t.mutating and t.status == "COMPLETED":
                prefix = t.idempotency_key.rsplit(":", 3)[0]
                seen[(prefix, t.args_hash)] += 1
        dup_side_effects = sum(n - 1 for n in seen.values() if n > 1)
        prevented = sum(1 for e in events if e.type == "tool.reconciled" and e.data.get("outcome") == "already_applied")
        max_attempts = self.settings.max_subtask_attempts
        loop_truth = _loop_ground_truth(sc, seed, max_attempts)
        # A stochastic agent that is wrong on every attempt cannot be completed: stopping safely is correct.
        expected = "safe_stop" if (sc.expected == "complete" and loop_truth) else sc.expected
        if expected == "complete":
            correct = completed and not violation and pass_rate == 1.0
        elif expected == "escalate":
            correct = escalated and not completed
        else:
            correct = (
                status in (TaskStatus.FAILED.value, TaskStatus.RECOVERY_PENDING.value, TaskStatus.CANCELLED.value)
                and not violation
            )
        return TrialResult(
            scenario=sc.name,
            config=config,
            seed=seed,
            expected=expected,
            status=status,
            completed=completed,
            correct=correct,
            criteria_pass_rate=round(pass_rate, 4),
            escalated=escalated,
            crashed=crashed,
            recovered_ok=crashed and completed and any(r.reason == "recovery" for r in runs),
            loop_detected=no_progress,
            loop_ground_truth=loop_truth,
            duplicate_action_blocks=dup_blocks,
            duplicate_side_effects=dup_side_effects,
            duplicates_prevented=prevented,
            latency_s=round(latency, 4),
            tokens=int(usage[0]),
            cost_usd=float(usage[1]),
            tool_failures=sum(1 for t in tools if t.status == "FAILED"),
            tool_denials=sum(1 for t in tools if t.status == "DENIED"),
            policy_violation=violation,
            retries=sum(1 for e in events if e.type == "retry.initiated" and e.actor == "supervisor"),
            notes=[f"error: {task.error.get('message')}"] if task.error else [],
        )


def summarize(results: list[TrialResult]) -> dict[str, Any]:
    by_cfg: dict[str, list[TrialResult]] = defaultdict(list)
    for r in results:
        by_cfg[r.config].append(r)
    out: dict[str, Any] = {}
    for cfg, rs in by_cfg.items():
        n = len(rs)
        completed = [r for r in rs if r.completed]
        crashed = [r for r in rs if r.crashed]
        tp = sum(1 for r in rs if r.loop_detected and r.loop_ground_truth)
        fp = sum(1 for r in rs if r.loop_detected and not r.loop_ground_truth)
        fn = sum(1 for r in rs if not r.loop_detected and r.loop_ground_truth)
        lat = [r.latency_s for r in rs]
        cost_total = sum(r.cost_usd for r in rs)
        out[cfg] = {
            "trials": n,
            "completion_rate": wilson(len(completed), n),
            "correct_outcome_rate": wilson(sum(r.correct for r in rs), n),
            "criteria_pass_rate_mean": bootstrap_mean([r.criteria_pass_rate for r in rs]),
            "recovery_success_rate": wilson(sum(r.recovered_ok for r in crashed), len(crashed)),
            "loop_detection": {
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "precision": (tp / (tp + fp)) if (tp + fp) else None,
                "recall": (tp / (tp + fn)) if (tp + fn) else None,
            },
            "duplicate_action_blocks": sum(r.duplicate_action_blocks for r in rs),
            "duplicate_side_effects": sum(r.duplicate_side_effects for r in rs),
            "duplicates_prevented_by_reconciliation": sum(r.duplicates_prevented for r in rs),
            "latency_s": {"mean": bootstrap_mean(lat), "p95": percentile(lat, 0.95)},
            "tokens_mean": bootstrap_mean([float(r.tokens) for r in rs]),
            "cost_per_successful_task_usd": (cost_total / len(completed)) if completed else None,
            "tool_failures": sum(r.tool_failures for r in rs),
            "tool_denials": sum(r.tool_denials for r in rs),
            "policy_violation_rate": wilson(sum(r.policy_violation for r in rs), n),
            "supervisor_retries": sum(r.retries for r in rs),
        }
    return out


def per_scenario(results: list[TrialResult]) -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = defaultdict(dict)
    groups: dict[tuple[str, str], list[TrialResult]] = defaultdict(list)
    for r in results:
        groups[(r.scenario, r.config)].append(r)
    for (sc, cfg), rs in sorted(groups.items()):
        table[sc][cfg] = {
            "correct": f"{sum(r.correct for r in rs)}/{len(rs)}",
            "completed": f"{sum(r.completed for r in rs)}/{len(rs)}",
            "violations": sum(r.policy_violation for r in rs),
            "statuses": sorted({r.status for r in rs}),
        }
    return table


async def run_benchmark(
    settings: Settings,
    *,
    trials: int,
    scenarios: list[str] | None = None,
    configs: list[str] | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    names = scenarios or sorted(SCENARIOS)
    cfgs = configs or list(CONFIGS)
    results: list[TrialResult] = []
    started = time.time()
    async with Harness(settings) as h:
        for name in names:
            for cfg in cfgs:
                for seed in range(trials):
                    r = await h.run_trial(SCENARIOS[name], cfg, seed)
                    results.append(r)
                    if progress:
                        progress(r)
    return {
        "meta": {
            "label": "SIMULATED AGENTS (deterministic fake LLM) - see docs/EVALUATION.md",
            "trials_per_cell": trials,
            "scenarios": names,
            "configs": {c: CONFIGS[c] for c in cfgs},
            "duration_s": round(time.time() - started, 1),
            "sandbox_backend": settings.sandbox_backend.value,
            "measured": [
                "latency_s",
                "criteria_pass_rate",
                "tool_failures",
                "tool_denials",
                "recoveries",
                "loop detections",
                "policy violations in delivered code",
            ],
            "simulated": ["agent behaviour", "tokens (estimated ~4 chars/token)", "cost (notional fake-llm price)"],
        },
        "summary": summarize(results),
        "per_scenario": per_scenario(results),
        "trials": [asdict(r) for r in results],
    }


def run_sync(settings: Settings, **kw: Any) -> dict[str, Any]:
    return asyncio.run(run_benchmark(settings, **kw))


_ = TERMINAL_STATES  # re-exported for scripts

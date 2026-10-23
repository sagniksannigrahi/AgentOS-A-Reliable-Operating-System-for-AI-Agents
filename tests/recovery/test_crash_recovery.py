"""Crash-injection tests: a worker 'dies' (SimulatedCrash: no cleanup, lease left behind) at each
of the five crash points; a second worker must resume from the persisted checkpoint, not restart.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.domain.errors import LeaseLostError, SimulatedCrash
from app.evaluation.scenarios import SCENARIOS
from app.persistence.models import AgentRun, CheckpointRef, ExecutionEvent, TaskRun, ToolInvocation
from tests.conftest import drain, expire_leases


async def _crash_then_recover(api, worker, settings, faults, **opts):
    body = SCENARIOS["calc_basic"].task_request(faults=faults, **opts)
    r = await api.client.post("/api/v1/tasks", json=body, headers=api.h())
    tid = r.json()["id"]
    with pytest.raises(SimulatedCrash):
        await drain(worker)
    async with api.state.db.session() as s:
        run = (await s.scalars(select(TaskRun).where(TaskRun.task_id == tid))).one()
        assert run.status == "RUNNING"  # the dead worker left its lease behind
    await expire_leases(settings)
    await drain(worker)
    detail = (await api.client.get(f"/api/v1/tasks/{tid}", headers=api.h())).json()
    return tid, detail


async def _events(api, tid, type_):
    async with api.state.db.session() as s:
        return (
            await s.scalars(select(ExecutionEvent).where(ExecutionEvent.task_id == tid, ExecutionEvent.type == type_))
        ).all()


@pytest.mark.parametrize(
    "fault, resumes_at",
    [
        ("crash:before_agent:coder", "execute_subtasks"),  # 1. before an agent starts
        ("crash:during_llm:coder", "execute_subtasks"),  # 2. during an LLM request
        ("crash:after_tool:write_file", "execute_subtasks"),  # 3. tool executed, result not persisted
        ("crash:after_node:execute_subtasks", "supervisor"),  # 4. between two workflow nodes
        ("crash:during_finalize", "finalize"),  # 5. during final result generation
    ],
)
async def test_recovers_from_checkpoint(api, worker, settings, fault, resumes_at):
    tid, detail = await _crash_then_recover(api, worker, settings, [fault])
    assert detail["status"] == "COMPLETED", detail["error"]
    runs = detail["runs"]
    assert [r["status"] for r in runs] == ["ABANDONED", "SUCCEEDED"]
    assert runs[1]["reason"] == "recovery" and runs[1]["recovered_from_run_id"] == runs[0]["id"]
    restored = await _events(api, tid, "checkpoint.restored")
    assert len(restored) == 1
    assert restored[0].data["next"] == [resumes_at]  # resumed mid-workflow, not from the start
    async with api.state.db.session() as s:
        planners = await s.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.task_id == tid, AgentRun.role == "planner")
        )
        assert planners == 1  # the plan was not regenerated
        restored_refs = await s.scalar(
            select(func.count())
            .select_from(CheckpointRef)
            .where(CheckpointRef.task_id == tid, CheckpointRef.restored.is_(True))
        )
        assert restored_refs == 1
        writes = (
            await s.scalars(
                select(ToolInvocation).where(ToolInvocation.task_id == tid, ToolInvocation.tool_name == "write_file")
            )
        ).all()
    # exactly one logical write of calc.py, never executed twice
    assert len(writes) == 1
    assert writes[0].status in ("COMPLETED", "RECONCILED")


async def test_after_tool_crash_is_reconciled_not_reexecuted(api, worker, settings):
    tid, detail = await _crash_then_recover(api, worker, settings, ["crash:after_tool:write_file"])
    assert detail["status"] == "COMPLETED"
    async with api.state.db.session() as s:
        w = (
            await s.scalars(
                select(ToolInvocation).where(ToolInvocation.task_id == tid, ToolInvocation.tool_name == "write_file")
            )
        ).one()
    assert w.status == "RECONCILED"
    assert w.attempts == 0 or w.attempts == 1
    rec = await _events(api, tid, "tool.reconciled")
    assert rec and rec[0].data["outcome"] == "already_applied"


async def test_replayed_agent_runs_are_marked(api, worker, settings):
    tid, detail = await _crash_then_recover(api, worker, settings, ["crash:during_llm:coder"])
    assert detail["status"] == "COMPLETED"
    async with api.state.db.session() as s:
        coder = (
            await s.scalars(
                select(AgentRun).where(
                    AgentRun.task_id == tid, AgentRun.role == "coder", AgentRun.subtask_key == "impl"
                )
            )
        ).one()
    assert coder.replay_count == 1 and coder.status == "SUCCEEDED"


async def test_recovery_disabled_fails_safely(api, worker, settings):
    _tid, detail = await _crash_then_recover(api, worker, settings, ["crash:after_tool:write_file"], recovery=False)
    assert detail["status"] == "FAILED"
    assert "recovery disabled" in detail["error"]["message"]


async def test_crash_loop_parks_task_for_human_then_resume(api, worker, settings):
    settings.max_recoveries = 0  # any recovery beyond the first crash is "too many"
    body = SCENARIOS["calc_basic"].task_request(faults=["crash:before_agent:coder"])
    tid = (await api.client.post("/api/v1/tasks", json=body, headers=api.h())).json()["id"]
    with pytest.raises(SimulatedCrash):
        await drain(worker)
    await expire_leases(settings)
    await drain(worker)
    detail = (await api.client.get(f"/api/v1/tasks/{tid}", headers=api.h())).json()
    assert detail["status"] == "RECOVERY_PENDING"
    r = await api.client.post(f"/api/v1/tasks/{tid}/resume", json={"reason": "investigated"}, headers=api.h())
    assert r.status_code == 202, r.text
    await drain(worker)
    detail = (await api.client.get(f"/api/v1/tasks/{tid}", headers=api.h())).json()
    assert detail["status"] == "COMPLETED", detail["error"]
    assert [r["reason"] for r in detail["runs"]] == ["initial", "recovery", "resume"]


async def test_zombie_worker_writes_are_fenced(api, worker, settings):
    """A worker whose lease was taken over cannot write anything for the task anymore."""
    from app.persistence.repository import claim_run, guard_lease

    body = SCENARIOS["calc_basic"].task_request()
    await api.client.post("/api/v1/tasks", json=body, headers=api.h())
    async with api.state.db.session() as s:
        first = await claim_run(s, "zombie", lease_seconds=30)
    assert first is not None
    await expire_leases(settings)
    async with api.state.db.session() as s:
        second = await claim_run(s, "rescuer", lease_seconds=30)
    assert second is not None and second.task_id == first.task_id and second.fencing_token == first.fencing_token + 1
    with pytest.raises(LeaseLostError):
        async with api.state.db.session() as s:
            await guard_lease(s, first.run_id, first.fencing_token)
    async with api.state.db.session() as s:
        await guard_lease(s, second.run_id, second.fencing_token)  # the new owner may write


async def test_only_one_active_run_per_task(api, settings):
    from sqlalchemy.exc import IntegrityError

    from app.domain.enums import RunReason
    from app.persistence.models import Task
    from app.persistence.repository import enqueue_run

    body = SCENARIOS["calc_basic"].task_request()
    tid = (await api.client.post("/api/v1/tasks", json=body, headers=api.h())).json()["id"]
    with pytest.raises(IntegrityError):
        async with api.state.db.session() as s:
            task = await s.get(Task, __import__("uuid").UUID(tid))
            await enqueue_run(s, task, RunReason.RESUME)


async def test_concurrent_claims_never_double_assign(api, settings):
    import asyncio

    from app.persistence.repository import claim_run

    for _ in range(4):
        await api.client.post("/api/v1/tasks", json=SCENARIOS["calc_basic"].task_request(), headers=api.h())

    async def claim(i):
        async with api.state.db.session() as s:
            return await claim_run(s, f"w{i}", 30)

    claims = [c for c in await asyncio.gather(*(claim(i) for i in range(8))) if c is not None]
    assert len(claims) == 4
    assert len({c.run_id for c in claims}) == 4

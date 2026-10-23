"""Workflow paths: approvals, rejection, cancellation, budgets, outages, invalid output, modes."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.evaluation.scenarios import SCENARIOS
from app.persistence.models import ExecutionEvent
from tests.conftest import drain


async def _create(api, body):
    r = await api.client.post("/api/v1/tasks", json=body, headers=api.h())
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _task(api, tid):
    return (await api.client.get(f"/api/v1/tasks/{tid}", headers=api.h())).json()


async def _pending(api, tid):
    r = await api.client.get(f"/api/v1/approvals?task_id={tid}", headers=api.h("approver"))
    return r.json()["items"]


async def _types(api, tid):
    async with api.state.db.session() as s:
        return [
            e.type
            for e in (
                await s.scalars(
                    select(ExecutionEvent).where(ExecutionEvent.task_id == tid).order_by(ExecutionEvent.seq)
                )
            ).all()
        ]


async def test_plan_approval_then_complete(api, worker):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(require_plan_approval=True))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "AWAITING_APPROVAL" and task["pending_approvals"] == 1
    assert task["runs"][-1]["status"] == "PAUSED"
    [appr] = await _pending(api, tid)
    assert appr["kind"] == "plan" and appr["task_title"]
    r = await api.client.post(
        f"/api/v1/approvals/{appr['id']}/decision", json={"decision": "approve"}, headers=api.h("approver")
    )
    assert r.status_code == 200
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "COMPLETED", task["error"]
    assert [r["reason"] for r in task["runs"]] == ["initial", "approval"]
    types = await _types(api, tid)
    assert "approval.requested" in types and "approval.decided" in types and "checkpoint.restored" not in types


async def test_plan_rejection_fails_task(api, worker):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(require_plan_approval=True))
    await drain(worker)
    [appr] = await _pending(api, tid)
    await api.client.post(
        f"/api/v1/approvals/{appr['id']}/decision",
        json={"decision": "reject", "comment": "no"},
        headers=api.h("approver"),
    )
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "FAILED" and "rejected" in task["error"]["message"]


async def test_ambiguous_request_escalates_to_human(api, worker):
    tid = await _create(api, SCENARIOS["ambiguous_spec"].task_request())
    await drain(worker)
    [appr] = await _pending(api, tid)
    assert "clarification" in appr["reason"] and appr["payload"]["question"]
    await api.client.post(
        f"/api/v1/approvals/{appr['id']}/decision", json={"decision": "reject"}, headers=api.h("approver")
    )
    await drain(worker)
    assert (await _task(api, tid))["status"] == "FAILED"


async def test_cancel_while_awaiting_approval(api, worker):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(require_plan_approval=True))
    await drain(worker)
    r = await api.client.post(f"/api/v1/tasks/{tid}/cancel", json={"reason": "changed my mind"}, headers=api.h())
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert await _pending(api, tid) == []


async def test_cancel_during_execution_stops_at_safe_point(api, worker, monkeypatch):
    """Cancellation requested while the coder is working stops before the next side effect."""
    from app.agents import coder as coder_mod

    tid = await _create(api, SCENARIOS["calc_basic"].task_request())
    original = coder_mod.run_tool_loop

    async def cancelling_loop(ctx, *a, **kw):
        r = await api.client.post(f"/api/v1/tasks/{tid}/cancel", headers=api.h())
        assert r.status_code == 202 and r.json()["status"] == "CANCEL_REQUESTED"
        return await original(ctx, *a, **kw)

    monkeypatch.setattr(coder_mod, "run_tool_loop", cancelling_loop)
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "CANCELLED"
    tools = (await api.client.get(f"/api/v1/tasks/{tid}/tools", headers=api.h())).json()["items"]
    assert not any(t["tool_name"] == "write_file" and t["status"] == "COMPLETED" for t in tools)


async def test_graph_step_budget(api, worker):
    tid = await _create(api, SCENARIOS["calc_bug_then_fix"].task_request(max_graph_steps=8))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "FAILED" and task["error"]["failure_class"] == "budget_exceeded"
    assert "budget.exceeded" in await _types(api, tid)


async def test_token_budget(api, worker):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(max_total_tokens=1000))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "FAILED" and "token budget" in task["error"]["message"]


async def test_llm_outage_pauses_then_resume_completes(api, worker):
    # 5 consecutive timeouts > llm_max_retries (3): the planner gives up; the task is parked.
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(faults=["llm_timeout:planner:4"]))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "RECOVERY_PENDING", task
    assert task["error"]["failure_class"] == "transient"
    r = await api.client.post(f"/api/v1/tasks/{tid}/resume", headers=api.h())
    assert r.status_code == 202
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "COMPLETED", task["error"]


async def test_invalid_llm_output_is_repaired(api, worker):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(faults=["llm_invalid:planner:2"]))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "COMPLETED"
    async with api.state.db.session() as s:
        retries = (
            await s.scalars(
                select(ExecutionEvent).where(ExecutionEvent.task_id == tid, ExecutionEvent.type == "retry.initiated")
            )
        ).all()
    assert sum(1 for e in retries if e.data.get("kind") == "invalid_output") == 2


async def test_persistently_invalid_output_fails(api, worker):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(faults=["llm_invalid:planner:9"]))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "FAILED" and task["error"]["failure_class"] == "invalid_output"


async def test_transient_tool_failure_is_retried(api, worker):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(faults=["tool_error:run_tests:1"]))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "COMPLETED"
    tools = (await api.client.get(f"/api/v1/tasks/{tid}/tools", headers=api.h())).json()["items"]
    assert any(t["tool_name"] == "run_tests" and t["attempts"] == 2 for t in tools)


@pytest.mark.parametrize("mode", ["unsupervised", "single_agent"])
async def test_baseline_modes_run(api, worker, mode):
    tid = await _create(api, SCENARIOS["calc_basic"].task_request(workflow_mode=mode))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "COMPLETED", task["error"]
    roles = {a["role"] for a in (await api.client.get(f"/api/v1/tasks/{tid}/agents", headers=api.h())).json()["items"]}
    if mode == "single_agent":
        assert roles == {"single_agent", "tester"}
    else:
        assert "security_auditor" not in roles


async def test_unsupervised_does_not_retry(api, worker):
    tid = await _create(api, SCENARIOS["calc_bug_then_fix"].task_request(workflow_mode="unsupervised"))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "FAILED"
    assert "retry.initiated" not in await _types(api, tid)


async def test_loop_detection_disabled_uses_full_budget(api, worker):
    tid = await _create(api, SCENARIOS["loop_no_progress"].task_request(loop_detection=False))
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "FAILED" and task["error"]["failure_class"] == "test_failure"
    types = await _types(api, tid)
    # without loop detection the supervisor exhausts every attempt, replans once, and exhausts again
    assert types.count("retry.initiated") == 4 and "replan.initiated" in types and "loop.detected" not in types


async def test_sensitive_tool_approval_inside_workflow(api, worker, monkeypatch):
    """A coder that edits a test file pauses the workflow until a human approves that exact write."""
    from app.evaluation import scenarios as sc_mod

    sc = sc_mod.SCENARIOS["calc_basic"]
    patched = sc_mod.Scenario(
        **{
            **sc.__dict__,
            "name": "calc_edits_tests",
            "solutions": {
                "impl": {
                    "calc.py": sc_mod.CALC_SOLUTION,
                    "tests/test_extra.py": "def test_extra():\n    assert True\n",
                },
                "verify": {},
            },
        }
    )
    monkeypatch.setitem(sc_mod.SCENARIOS, "calc_edits_tests", patched)
    tid = await _create(api, patched.task_request())
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "AWAITING_APPROVAL"
    [appr] = await _pending(api, tid)
    assert appr["kind"] == "tool" and appr["payload"]["args"]["path"] == "tests/test_extra.py"
    await api.client.post(
        f"/api/v1/approvals/{appr['id']}/decision", json={"decision": "approve"}, headers=api.h("approver")
    )
    await drain(worker)
    task = await _task(api, tid)
    assert task["status"] == "COMPLETED", task["error"]
    # the approved test edit is recorded and not flagged as tampering by the auditor
    sec = [
        e
        for e in (await api.client.get(f"/api/v1/tasks/{tid}/events?type_prefix=security", headers=api.h())).json()[
            "items"
        ]
    ]
    assert all("test_or_ci_tampering" not in str(e["data"]) for e in sec)

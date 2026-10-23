"""Run each scripted scenario through the supervised workflow and report the outcome."""

from __future__ import annotations

import pytest

from app.evaluation.scenarios import SCENARIOS
from tests.conftest import drain, expire_leases


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario_smoke(api, worker, settings, name):
    from app.domain.errors import SimulatedCrash

    sc = SCENARIOS[name]
    r = await api.client.post("/api/v1/tasks", json=sc.task_request(), headers=api.h())
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    for _ in range(6):
        try:
            await drain(worker)
            break
        except SimulatedCrash:
            await expire_leases(settings)
    detail = (await api.client.get(f"/api/v1/tasks/{tid}", headers=api.h())).json()
    events = (await api.client.get(f"/api/v1/tasks/{tid}/events?limit=500", headers=api.h())).json()["items"]
    print(f"\n### {name}: {detail['status']} error={detail['error']}")
    for e in events:
        if e["type"] in (
            "supervisor.decision",
            "tool.denied",
            "loop.detected",
            "conflict.detected",
            "error.detected",
            "retry.initiated",
            "recovery.started",
            "checkpoint.restored",
            "approval.requested",
            "security.reviewed",
            "test.executed",
            "task.status_changed",
            "tool.reconciled",
        ):
            print(f"  {e['seq']:>3} {e['type']:<22} {e['message'][:150]}")

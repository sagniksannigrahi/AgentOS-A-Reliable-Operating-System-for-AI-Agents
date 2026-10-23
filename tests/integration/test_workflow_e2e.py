"""End-to-end: API -> queue -> worker -> LangGraph -> agents -> tools -> sandbox -> results."""

from __future__ import annotations

from tests.conftest import drain


async def _create(api, body):
    r = await api.client.post("/api/v1/tasks", json=body, headers=api.h())
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _get(api, tid, path=""):
    r = await api.client.get(f"/api/v1/tasks/{tid}{path}", headers=api.h())
    assert r.status_code == 200, r.text
    return r.json()


async def test_calc_basic_completes(api, worker):
    from app.evaluation.scenarios import SCENARIOS

    tid = await _create(api, SCENARIOS["calc_basic"].task_request())
    assert await drain(worker) == 1
    task = await _get(api, tid)
    assert task["status"] == "COMPLETED", task["error"]
    assert all(c["passed"] for c in task["result"]["criteria"])
    assert task["plan"]["version"] == 1 and len(task["plan"]["subtasks"]) == 2
    assert task["usage"]["calls"] > 0 and task["usage"]["estimated_tokens"] > 0
    assert task["usage"]["provider_reported_tokens"] == 0
    statuses = [t["to_status"] for t in await _get(api, tid, "/transitions")]
    assert statuses[0] == "PLANNING" and statuses[-1] == "COMPLETED"
    assert "RUNNING" in statuses and "VALIDATING" in statuses
    agents = (await _get(api, tid, "/agents"))["items"]
    roles = {a["role"] for a in agents}
    assert roles == {"planner", "coder", "tester", "security_auditor"}
    tools = (await _get(api, tid, "/tools"))["items"]
    assert any(t["tool_name"] == "write_file" and t["status"] == "COMPLETED" for t in tools)
    assert any(t["tool_name"] == "run_tests" and t["status"] == "COMPLETED" for t in tools)
    arts = (await _get(api, tid, "/artifacts"))["items"]
    assert {a["kind"] for a in arts} >= {"diff", "test_report", "plan", "summary"}
    diff = next(a for a in arts if a["kind"] == "diff")
    content = (await _get(api, tid, f"/artifacts/{diff['id']}"))["content"]
    assert "+    if b == 0:" in content
    events = (await _get(api, tid, "/events?limit=500"))["items"]
    types = [e["type"] for e in events]
    for t in (
        "task.created",
        "plan.generated",
        "agent.started",
        "tool.allowed",
        "test.executed",
        "checkpoint.written",
        "security.reviewed",
        "task.completed",
    ):
        assert t in types, t
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

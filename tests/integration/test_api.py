"""API integration tests against real PostgreSQL."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import calc_task


async def test_health(api):
    r = await api.client.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    r = await api.client.get("/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["migrations"] == "ok"
    assert body["checks"]["checkpointer"] == "ok"
    assert body["checks"]["redis"] == "disabled"


async def test_metrics_endpoint(api):
    await api.client.get("/health/live")
    r = await api.client.get("/metrics/")
    assert r.status_code == 200
    assert "agentos_http_requests_total" in r.text


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"X-API-Key": "aos_wrong"}]
)
async def test_auth_required(api, headers):
    r = await api.client.get("/api/v1/tasks", headers=headers)
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "unauthenticated"
    assert err["request_id"]


async def test_scope_enforced(api):
    r = await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h("reader"))
    assert r.status_code == 403
    assert r.json()["error"]["details"]["missing"] == ["tasks:write"]


async def test_x_api_key_header_works(api):
    r = await api.client.get("/api/v1/tasks", headers={"X-API-Key": api.keys["reader"]})
    assert r.status_code == 200


async def test_create_get_list_task(api):
    r = await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h())
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["status"] == "CREATED"
    assert r.headers["location"] == f"/api/v1/tasks/{task['id']}"
    assert len(task["runs"]) == 1 and task["runs"][0]["status"] == "QUEUED"
    assert task["trace_id"] and len(task["trace_id"]) == 32

    r = await api.client.get(f"/api/v1/tasks/{task['id']}", headers=api.h("reader"))
    assert r.status_code == 200 and r.json()["id"] == task["id"]

    r = await api.client.get("/api/v1/tasks?status=CREATED&limit=5", headers=api.h("reader"))
    page = r.json()
    assert page["total"] == 1 and page["limit"] == 5 and page["items"][0]["id"] == task["id"]

    r = await api.client.get("/api/v1/tasks?status=COMPLETED", headers=api.h("reader"))
    assert r.json()["total"] == 0

    r = await api.client.get(f"/api/v1/tasks/{task['id']}/events", headers=api.h("reader"))
    types = [e["type"] for e in r.json()["items"]]
    assert types == ["task.created", "run.queued"]


async def test_pagination(api):
    for i in range(5):
        body = calc_task()
        body["title"] = f"task {i}"
        assert (await api.client.post("/api/v1/tasks", json=body, headers=api.h())).status_code == 201
    r = await api.client.get("/api/v1/tasks?limit=2&offset=2", headers=api.h())
    page = r.json()
    assert page["total"] == 5 and len(page["items"]) == 2 and page["offset"] == 2
    r = await api.client.get("/api/v1/tasks?limit=1000", headers=api.h())
    assert r.status_code == 422


async def test_idempotent_create(api):
    h = {**api.h(), "Idempotency-Key": "abc-123"}
    r1 = await api.client.post("/api/v1/tasks", json=calc_task(), headers=h)
    r2 = await api.client.post("/api/v1/tasks", json=calc_task(), headers=h)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    other = calc_task()
    other["title"] = "different"
    r3 = await api.client.post("/api/v1/tasks", json=other, headers=h)
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "mutate, code, status",
    [
        (lambda b: b.update(workspace_seed="does-not-exist"), "unknown_workspace_seed", 422),
        (lambda b: b.update(workspace_seed="../etc"), "validation_error", 422),
        (lambda b: b["acceptance_criteria"][0].update(command=["bash", "-c", "rm -rf /"]), "command_not_allowed", 422),
        (lambda b: b.update(acceptance_criteria=[]), "validation_error", 422),
        (lambda b: b.update(title=""), "validation_error", 422),
        (lambda b: b.update(unexpected="x"), "validation_error", 422),
        (lambda b: b["options"].update(faults=["crash:everywhere"]), "validation_error", 422),
        (lambda b: b["options"].update(llm_scenario="nope"), "unknown_scenario", 422),
    ],
)
async def test_create_validation(api, mutate, code, status):
    body = calc_task()
    mutate(body)
    r = await api.client.post("/api/v1/tasks", json=body, headers=api.h())
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


async def test_evaluation_options_rejected_outside_eval_mode(pg_url, tmp_dirs, clean_db):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from app.persistence.repository import create_api_key
    from tests.conftest import make_settings

    app = create_app(make_settings(pg_url, tmp_dirs, evaluation_mode=False))
    async with app.router.lifespan_context(app):
        async with app.state.agentos.db.session() as s:
            _, key = await create_api_key(s, "k", ["tasks:write", "tasks:read"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            for opts in ({"workflow_mode": "unsupervised"}, {"faults": ["crash:during_finalize"]}, {"recovery": False}):
                r = await c.post("/api/v1/tasks", json=calc_task(**opts), headers={"Authorization": f"Bearer {key}"})
                assert r.status_code == 403, opts
                assert r.json()["error"]["code"] == "evaluation_features_disabled"
            r = await c.post("/api/v1/tasks", json=calc_task(), headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 201


async def test_not_found_and_bad_uuid(api):
    r = await api.client.get(f"/api/v1/tasks/{uuid.uuid4()}", headers=api.h())
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = await api.client.get("/api/v1/tasks/not-a-uuid", headers=api.h())
    assert r.status_code == 422
    r = await api.client.get("/api/v1/nope", headers=api.h())
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


async def test_cancel_queued_task(api):
    task = (await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h())).json()
    r = await api.client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=api.h())
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    detail = (await api.client.get(f"/api/v1/tasks/{task['id']}", headers=api.h())).json()
    assert detail["status"] == "CANCELLED"
    assert detail["runs"][0]["status"] == "CANCELLED"
    r = await api.client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=api.h())
    assert r.status_code == 409
    transitions = (await api.client.get(f"/api/v1/tasks/{task['id']}/transitions", headers=api.h())).json()
    assert [(t["from_status"], t["to_status"]) for t in transitions] == [("CREATED", "CANCELLED")]


async def test_resume_rejected_for_non_resumable(api):
    task = (await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h())).json()
    r = await api.client.post(f"/api/v1/tasks/{task['id']}/resume", headers=api.h())
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"


async def test_approvals_listing_requires_scope(api):
    r = await api.client.get("/api/v1/approvals", headers=api.h("reader"))
    assert r.status_code == 403
    r = await api.client.get("/api/v1/approvals", headers=api.h("approver"))
    assert r.status_code == 200 and r.json()["items"] == []
    r = await api.client.post(
        f"/api/v1/approvals/{uuid.uuid4()}/decision", json={"decision": "approve"}, headers=api.h("approver")
    )
    assert r.status_code == 404
    r = await api.client.post(
        f"/api/v1/approvals/{uuid.uuid4()}/decision", json={"decision": "maybe"}, headers=api.h("approver")
    )
    assert r.status_code == 422


async def test_meta_and_stats(api):
    r = await api.client.get("/api/v1/meta", headers=api.h())
    meta = r.json()
    assert {s["name"] for s in meta["seeds"]} >= {"calculator", "textkit"}
    assert meta["llm_provider"] == "fake"
    r = await api.client.get("/api/v1/stats", headers=api.h())
    assert r.status_code == 200 and r.json()["tasks"]["total"] == 0


async def test_admin_scope_grants_everything(api):
    r = await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h("admin"))
    assert r.status_code == 201

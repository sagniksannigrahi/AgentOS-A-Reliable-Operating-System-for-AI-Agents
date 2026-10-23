"""Redis behaviour, dependency outages, idempotent events and observability correlation."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.evaluation.scenarios import SCENARIOS
from app.persistence.models import ExecutionEvent, SpanRecord, Task
from app.persistence.redis_client import RedisCoordinator
from app.persistence.repository import record_event
from tests.conftest import calc_task, drain

# ----------------------------------------------------------------------------------- redis


async def test_rate_limit_with_redis():
    rc = RedisCoordinator(fakeredis.aioredis.FakeRedis(decode_responses=True))
    results = [await rc.allow("k", 3) for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert await rc.ping()


async def test_wakeup_signal():
    rc = RedisCoordinator(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await rc.notify_work()
    assert await rc.wait_for_work(1) is True


async def test_cache_roundtrip():
    rc = RedisCoordinator(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await rc.cache_set("x", "1", 10)
    assert await rc.cache_get("x") == "1"


async def test_redis_down_fails_open():
    rc = RedisCoordinator.from_url("redis://127.0.0.1:1/0")  # nothing listens on port 1
    assert await rc.ping() is False
    assert await rc.allow("k", 1) is True  # rate limiting degrades open; correctness unaffected
    await rc.notify_work()
    assert await rc.wait_for_work(1) is False
    assert await rc.cache_get("x") is None


async def test_api_rate_limit_enforced(api):
    api.state.redis = RedisCoordinator(fakeredis.aioredis.FakeRedis(decode_responses=True))
    api.state.settings.task_create_rate_per_minute = 2
    codes = [(await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h())).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


async def test_api_works_with_redis_down(api):
    api.state.redis = RedisCoordinator.from_url("redis://127.0.0.1:1/0")
    r = await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h())
    assert r.status_code == 201
    r = await api.client.get("/health/ready")
    assert r.status_code == 200 and r.json()["degraded"] is True


# --------------------------------------------------------------------------- outages / dedup


async def test_worker_survives_database_outage(settings, monkeypatch):
    from app.worker import main as worker_main

    w = worker_main.Worker(settings, worker_id="outage")
    await w.start()
    calls = {"n": 0}
    real = worker_main.Worker.run_once

    async def flaky(self):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        self.stopping.set()
        return await real(self)

    monkeypatch.setattr(worker_main.Worker, "run_once", flaky)
    monkeypatch.setattr(worker_main, "backoff_delay", lambda *a, **k: 0.01)
    try:
        await asyncio.wait_for(w._loop(0), timeout=10)
    finally:
        await w.close()
    assert calls["n"] == 3  # backed off twice, then recovered


async def test_unreachable_database_reports_not_ready(pg_url, tmp_dirs, clean_db):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from tests.conftest import make_settings

    bad = make_settings(
        pg_url, tmp_dirs, persist_spans=False, database_url="postgresql+psycopg://nobody:x@127.0.0.1:1/none"
    )
    app = create_app(bad)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/health/ready")
            assert r.status_code == 503 and r.json()["status"] == "not_ready"
            assert (await c.get("/health/live")).status_code == 200


async def test_duplicate_events_are_ignored(api):
    tid = uuid.UUID((await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h())).json()["id"])
    async with api.state.db.session() as s:
        first = await record_event(s, tid, "x.test", idempotency_key="dup-1")
        second = await record_event(s, tid, "x.test", idempotency_key="dup-1")
    assert first is not None and second is None
    async with api.state.db.session() as s:
        n = len((await s.scalars(select(ExecutionEvent).where(ExecutionEvent.idempotency_key == "dup-1"))).all())
    assert n == 1


# ---------------------------------------------------------------------------- observability


async def test_trace_correlates_api_and_worker(api, worker):
    from app.observability.tracing import flush_spans

    r = await api.client.post(
        "/api/v1/tasks",
        json=SCENARIOS["calc_basic"].task_request(),
        headers={**api.h(), "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
    )
    tid = r.json()["id"]
    assert r.json()["trace_id"] == "0af7651916cd43dd8448eb211c80319c"  # incoming context honoured
    await drain(worker)
    flush_spans()
    spans = (await api.client.get(f"/api/v1/tasks/{tid}/trace", headers=api.h())).json()
    names = {s["name"] for s in spans}
    assert any(n.startswith("HTTP POST") for n in names)
    assert "task run" in names and "node supervisor" in names
    assert any(n.startswith("agent ") for n in names) and any(n.startswith("tool ") for n in names)
    assert any(n.startswith("llm ") for n in names)
    assert {s["trace_id"] for s in spans} == {"0af7651916cd43dd8448eb211c80319c"}
    by_id = {s["span_id"]: s for s in spans}
    run_span = next(s for s in spans if s["name"] == "task run")
    ancestors, cur = [], run_span
    while cur.get("parent_span_id") in by_id:
        cur = by_id[cur["parent_span_id"]]
        ancestors.append(cur["name"])
    assert any(n.startswith("HTTP POST") for n in ancestors)  # worker run is a descendant of the HTTP request
    # events carry the same trace id
    events = (await api.client.get(f"/api/v1/tasks/{tid}/events?limit=500", headers=api.h())).json()["items"]
    worker_events = [e for e in events if e["type"] == "agent.started"]
    assert worker_events and all(e["trace_id"] == "0af7651916cd43dd8448eb211c80319c" for e in worker_events)


async def test_logs_are_json_with_ids_and_redacted(capsys):
    from app.observability.logging import bind_context, clear_context, configure_logging, get_logger
    from app.observability.tracing import start_span

    configure_logging("INFO", json=True)
    with start_span("test span"):
        bind_context(task_id="t-1")
        get_logger("t").info(
            "hello", api_key="sk-" + "ant-api03-zzzzzzzzzzzzzzzzzzzzzzzz", note="Bearer abcdefghijklmnopq"
        )
        clear_context()
    line = [ln for ln in capsys.readouterr().out.splitlines() if '"hello"' in ln][-1]
    rec = json.loads(line)
    assert rec["task_id"] == "t-1" and len(rec["trace_id"]) == 32 and len(rec["span_id"]) == 16
    assert rec["api_key"] == "[REDACTED]" and "abcdefghijklmnopq" not in line
    logging.getLogger().handlers.clear()


async def test_stats_reflect_real_runs(api, worker):
    for name in ("calc_basic", "loop_no_progress"):
        await api.client.post("/api/v1/tasks", json=SCENARIOS[name].task_request(), headers=api.h())
    await drain(worker)
    stats = (await api.client.get("/api/v1/stats", headers=api.h())).json()
    assert stats["tasks"]["by_status"] == {"COMPLETED": 1, "FAILED": 1}
    assert stats["rates"]["completion_rate"] == 0.5
    assert stats["events"]["loop.detected"] >= 1
    assert stats["usage"]["estimated"]["input_tokens"] > 0
    assert [a["role"] for a in stats["latency"]["agents"]]


async def test_metrics_exposed_after_run(api, worker):
    await api.client.post("/api/v1/tasks", json=SCENARIOS["calc_basic"].task_request(), headers=api.h())
    await drain(worker)
    text = (await api.client.get("/metrics/")).text
    for metric in (
        "agentos_tasks_finished_total",
        "agentos_tool_calls_total",
        "agentos_llm_tokens_total",
        "agentos_agent_duration_seconds",
        "agentos_checkpoints_written_total",
    ):
        assert metric in text


@pytest.mark.parametrize("bad", [{"task_id": "x"}])
async def test_task_table_untouched_by_bad_ids(api, bad):
    async with api.state.db.session() as s:
        assert (await s.scalars(select(Task))).all() == []
    assert (await api.client.get(f"/api/v1/tasks/{bad['task_id']}/trace", headers=api.h())).status_code == 422


async def test_span_rows_have_task_ids(api, worker):
    from app.observability.tracing import flush_spans

    tid = (await api.client.post("/api/v1/tasks", json=SCENARIOS["calc_basic"].task_request(), headers=api.h())).json()[
        "id"
    ]
    await drain(worker)
    flush_spans()
    async with api.state.db.session() as s:
        rows = (await s.scalars(select(SpanRecord).where(SpanRecord.task_id == uuid.UUID(tid)))).all()
    assert len(rows) > 20

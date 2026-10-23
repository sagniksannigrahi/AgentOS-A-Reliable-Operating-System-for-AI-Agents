"""Shared fixtures.

Integration tests run against a real PostgreSQL started with ``pgserver`` (bundled binaries, no
Docker needed). Set ``AGENTOS_TEST_DATABASE_URL`` to use an existing server instead (CI may do
this with a service container). Redis-backed behavior uses ``fakeredis``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from app.config.settings import PROJECT_ROOT, Settings

TABLES = (
    "spans",
    "execution_events",
    "llm_usage",
    "approval_decisions",
    "approval_requests",
    "checkpoint_refs",
    "artifacts",
    "tool_invocations",
    "subtask_dependencies",
    "subtasks",
    "plans",
    "agent_runs",
    "state_transitions",
    "task_runs",
    "tasks",
    "api_keys",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
)


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    external = os.environ.get("AGENTOS_TEST_DATABASE_URL")
    if external:
        from app.cli import migrate

        migrate(external)
        yield external
        return
    import pgserver

    data_dir = tempfile.mkdtemp(prefix="agentos-test-pg-")
    server = pgserver.get_server(data_dir, cleanup_mode="delete")
    base = server.get_uri()  # postgresql://postgres:@/postgres?host=/tmp/...
    # pgserver.psql() shells out and breaks on paths with spaces; use psycopg directly.
    import psycopg

    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute("CREATE DATABASE agentos_test")
    url = base.replace("postgresql://", "postgresql+psycopg://", 1).replace("/postgres?", "/agentos_test?", 1)
    from app.cli import migrate

    migrate(url)
    try:
        yield url
    finally:
        from app.observability.tracing import flush_spans

        flush_spans()
        server.cleanup()
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def clean_db(pg_url: str) -> Iterator[None]:
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    engine.dispose()
    yield


@pytest.fixture
def tmp_dirs() -> Iterator[dict[str, Path]]:
    # Short temp path without spaces (the project path contains a space).
    root = Path(tempfile.mkdtemp(prefix="agentos-ws-"))
    dirs = {"workspaces": root / "workspaces", "artifacts": root / "artifacts"}
    for d in dirs.values():
        d.mkdir(parents=True)
    yield dirs
    shutil.rmtree(root, ignore_errors=True)


def make_settings(pg_url: str, tmp: dict[str, Path], **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": pg_url,
        "redis_url": None,
        "evaluation_mode": True,
        "sandbox_backend": "local",
        "workspace_root": tmp["workspaces"],
        "artifact_root": tmp["artifacts"],
        "seed_repos_dir": PROJECT_ROOT / "fixtures" / "repos",
        "persist_spans": True,
        "log_json": True,
        "log_level": "WARNING",
        "lease_seconds": 30,
        "heartbeat_interval_s": 1,
        "retry_base_delay_s": 0.01,
        "retry_max_delay_s": 0.05,
        "sandbox_timeout_s": 60,
        "llm_provider": "fake",
        "task_create_rate_per_minute": 1000,
        "db_pool_size": 5,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
def settings(pg_url: str, tmp_dirs: dict[str, Path], clean_db: None) -> Settings:
    return make_settings(pg_url, tmp_dirs)


class ApiHarness:
    def __init__(self, client: AsyncClient, keys: dict[str, str], app: Any) -> None:
        self.client = client
        self.keys = keys
        self.app = app

    def h(self, who: str = "operator") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[who]}"}

    @property
    def state(self) -> Any:
        return self.app.state.agentos


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[ApiHarness]:
    from app.main import create_app
    from app.persistence.repository import create_api_key

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        state = app.state.agentos
        keys: dict[str, str] = {}
        async with state.db.session() as s:
            _, keys["operator"] = await create_api_key(s, "operator", ["tasks:read", "tasks:write", "approvals:read"])
            _, keys["approver"] = await create_api_key(
                s, "approver", ["tasks:read", "approvals:read", "approvals:decide"]
            )
            _, keys["reader"] = await create_api_key(s, "reader", ["tasks:read"])
            _, keys["admin"] = await create_api_key(s, "admin", ["admin"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield ApiHarness(client, keys, app)


def calc_task(**options: Any) -> dict[str, Any]:
    return {
        "title": "Implement multiply and divide",
        "description": "Implement multiply and divide in calc.py. divide(a, 0) must raise "
        "ValueError('division by zero').",
        "workspace_seed": "calculator",
        "acceptance_criteria": [
            {"id": "tests", "description": "all tests pass", "kind": "tests_pass", "command": ["pytest", "-q"]},
            {
                "id": "no-todo",
                "description": "no NotImplementedError left",
                "kind": "file_not_contains",
                "path": "calc.py",
                "text": "NotImplementedError",
            },
        ],
        "options": options,
    }


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Any]:
    from app.worker.main import Worker

    w = Worker(settings, worker_id="test-worker")
    await w.start()
    try:
        yield w
    finally:
        await w.close()


async def drain(worker: Any, max_runs: int = 20) -> int:
    """Execute queued runs until none are left. Returns the number executed."""
    n = 0
    while n < max_runs and await worker.run_once():
        n += 1
    return n


async def expire_leases(settings: Settings) -> None:
    """Simulate time passing after a worker died: its lease is now expired."""
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(settings.database_url)
    async with eng.begin() as c:
        await c.execute(
            sql_text("UPDATE task_runs SET lease_expires_at = now() - interval '1 second' WHERE status = 'RUNNING'")
        )
    await eng.dispose()

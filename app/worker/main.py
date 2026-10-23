"""Worker process: claims runs from the PostgreSQL queue and executes them."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.exc import DBAPIError, OperationalError

from app.config.settings import Settings, get_settings
from app.domain.errors import SimulatedCrash
from app.observability.logging import configure_logging, get_logger
from app.observability.tracing import flush_spans, setup_tracing
from app.persistence.db import Database
from app.persistence.redis_client import RedisCoordinator
from app.persistence.repository import claim_run
from app.supervisor.retry import backoff_delay
from app.worker.runner import WorkflowRunner

log = get_logger(__name__)


class Worker:
    def __init__(self, settings: Settings, worker_id: str | None = None) -> None:
        self.settings = settings
        self.worker_id = (
            worker_id or settings.worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        )
        self.db = Database(settings)
        self.redis = RedisCoordinator.from_url(settings.redis_url)
        self.pool: AsyncConnectionPool | None = None
        self.runner: WorkflowRunner | None = None
        self.stopping = asyncio.Event()

    async def start(self) -> None:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        self.pool = AsyncConnectionPool(
            self.settings.psycopg_conninfo,
            min_size=1,
            max_size=max(4, self.settings.worker_concurrency * 2),
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await self.pool.open()
        saver = AsyncPostgresSaver(self.pool)  # type: ignore[arg-type]
        await saver.setup()
        self.runner = WorkflowRunner(self.settings, self.db, saver, self.redis, self.worker_id)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
        await self.redis.close()
        await self.db.dispose()

    async def run_once(self) -> bool:
        """Claim and execute at most one run. Returns True if a run was executed."""
        assert self.runner is not None, "call start() first"
        async with self.db.session() as s:
            claimed = await claim_run(s, self.worker_id, self.settings.lease_seconds)
        if claimed is None:
            return False
        await self.runner.execute(claimed)
        return True

    async def _loop(self, n: int) -> None:
        failures = 0
        while not self.stopping.is_set():
            try:
                worked = await self.run_once()
                failures = 0
            except SimulatedCrash as crash:
                # Evaluation-mode fault injection: behave like a dead worker for that run (its lease
                # is left to expire and another loop/worker recovers it) but keep this loop alive.
                log.warning("simulated crash: abandoning run without cleanup", fault=str(crash), loop=n)
                continue
            except (OperationalError, DBAPIError, OSError) as exc:
                failures += 1
                delay = backoff_delay(failures, 0.5, 15.0)
                log.warning(
                    "worker loop: database unavailable; backing off",
                    error=str(exc)[:200],
                    delay_s=round(delay, 2),
                    loop=n,
                )
                await asyncio.sleep(delay)
                continue
            if not worked:
                woke = await self.redis.wait_for_work(self.settings.poll_interval_s)
                if not woke:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), timeout=self.settings.poll_interval_s)

    async def run_forever(self) -> None:
        loops = [asyncio.create_task(self._loop(i)) for i in range(max(1, self.settings.worker_concurrency))]
        await self.stopping.wait()
        log.info("shutdown requested; waiting for running tasks", grace_s=self.settings.shutdown_grace_s)
        _done, pending = await asyncio.wait(loops, timeout=self.settings.shutdown_grace_s)
        for t in pending:
            t.cancel()  # runs release their lease so another worker resumes them from a checkpoint
        await asyncio.gather(*pending, return_exceptions=True)


async def _main() -> None:
    settings = get_settings()
    worker = Worker(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stopping.set)
    await worker.start()
    log.info(
        "worker started",
        worker_id=worker.worker_id,
        concurrency=settings.worker_concurrency,
        provider=settings.llm_provider.value,
        sandbox=settings.sandbox_backend.value,
    )
    try:
        await worker.run_forever()
    finally:
        await worker.close()
        flush_spans()


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    setup_tracing(
        f"{settings.otel_service_name}-worker",
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        database_url=settings.database_url if settings.persist_spans else None,
    )
    from prometheus_client import start_http_server

    start_http_server(settings.worker_metrics_port)
    asyncio.run(_main())


if __name__ == "__main__":
    run()

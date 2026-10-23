"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from prometheus_client import make_asgi_app
from sqlalchemy import func, select

from app.api.deps import AppState
from app.api.errors import install_error_handlers
from app.api.routes_misc import approvals, health, meta
from app.api.routes_tasks import router as tasks_router
from app.config.settings import Settings, get_settings
from app.observability import metrics
from app.observability.logging import bind_context, clear_context, configure_logging, get_logger
from app.observability.tracing import setup_tracing, start_span
from app.persistence.db import Database
from app.persistence.models import ApiKey
from app.persistence.redis_client import RedisCoordinator
from app.persistence.repository import ALL_SCOPES, create_api_key

log = get_logger(__name__)


async def _bootstrap_key(state: AppState) -> None:
    secret = state.settings.bootstrap_api_key
    if secret is None:
        return
    async with state.db.session() as s:
        count = await s.scalar(select(func.count()).select_from(ApiKey))
        if count:
            return
        await create_api_key(s, "bootstrap", list(ALL_SCOPES), plaintext=secret.get_secret_value())
    log.warning("created bootstrap API key from AGENTOS_BOOTSTRAP_API_KEY (development only)")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_json)
        setup_tracing(
            f"{settings.otel_service_name}-api",
            otlp_endpoint=settings.otel_exporter_otlp_endpoint,
            database_url=settings.database_url if settings.persist_spans else None,
        )
        state = AppState(settings=settings, db=Database(settings), redis=RedisCoordinator.from_url(settings.redis_url))
        app.state.agentos = state
        await _bootstrap_key(state)
        log.info("api started", provider=settings.llm_provider.value, sandbox=settings.sandbox_backend.value)
        try:
            yield
        finally:
            await state.redis.close()
            await state.db.dispose()

    app = FastAPI(
        title="AgentOS API",
        version="0.1.0",
        description="Reliable, observable orchestration for multi-agent AI workflows.",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "X-API-Key", "Content-Type", "Idempotency-Key", "traceparent"],
        expose_headers=["X-Request-ID", "traceparent", "Location"],
    )

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        if len(request_id) > 64:
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        clear_context()
        bind_context(request_id=request_id)
        parent = propagate.extract(dict(request.headers))
        started = time.perf_counter()
        status = 500
        with start_span(
            f"HTTP {request.method}",
            kind=SpanKind.SERVER,
            context=parent,
            attributes={"http.method": request.method, "http.target": request.url.path},
        ) as span:
            try:
                response = await call_next(request)
                status = response.status_code
            finally:
                route = request.scope.get("route")
                route_path = getattr(route, "path", "unmatched")
                span.update_name(f"HTTP {request.method} {route_path}")
                span.set_attribute("http.route", route_path)
                span.set_attribute("http.status_code", status)
                if route_path != "/metrics":
                    metrics.HTTP_REQUESTS.labels(request.method, route_path, str(status)).inc()
                    metrics.HTTP_LATENCY.labels(request.method, route_path).observe(time.perf_counter() - started)
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
        response.headers["X-Request-ID"] = request_id
        if "traceparent" in carrier:
            response.headers["traceparent"] = carrier["traceparent"]
        return response

    app.include_router(health)
    app.include_router(tasks_router)
    app.include_router(approvals)
    app.include_router(meta)
    app.mount("/metrics", make_asgi_app())
    return app


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:create_app", factory=True, host=s.api_host, port=s.api_port, log_config=None)

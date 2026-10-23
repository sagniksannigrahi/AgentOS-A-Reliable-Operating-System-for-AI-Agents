"""OpenTelemetry tracing setup.

Spans are exported to:
* an OTLP endpoint (Jaeger / Tempo / any collector) when ``AGENTOS_OTEL_EXPORTER_OTLP_ENDPOINT`` is set;
* the ``spans`` table (when ``AGENTOS_PERSIST_SPANS=true``) so the dashboard can show a correlated
  trace for each task without extra infrastructure.

Correlation: the API stores the W3C ``traceparent`` of the request that created a task on the
task and its runs. Workers start their spans as children of that context, so HTTP request →
worker run → graph node → agent → LLM call / tool call form one trace.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.observability.logging import get_logger
from app.security.redaction import redact

log = get_logger(__name__)
TASK_ATTR = "agentos.task_id"

_lock = threading.Lock()
_provider: TracerProvider | None = None
_configured_exporters: set[str] = set()


class DbSpanExporter(SpanExporter):
    """Writes finished spans to PostgreSQL. Failures are logged, never raised into the app."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, pool_size=2, max_overflow=2, pool_pre_ping=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        from app.persistence.models import SpanRecord

        rows: list[dict[str, Any]] = []
        for s in spans:
            ctx = s.get_span_context()
            if ctx is None or s.start_time is None or s.end_time is None:
                continue
            attrs = dict(s.attributes or {})
            task_id = attrs.get(TASK_ATTR)
            rows.append(
                {
                    "span_id": format(ctx.span_id, "016x"),
                    "trace_id": format(ctx.trace_id, "032x"),
                    "parent_span_id": format(s.parent.span_id, "016x") if s.parent else None,
                    "name": s.name[:200],
                    "kind": s.kind.name if s.kind else "INTERNAL",
                    "service": str(s.resource.attributes.get("service.name", "agentos")),
                    "task_id": task_id if isinstance(task_id, str) and len(task_id) == 36 else None,
                    "start_time": datetime.fromtimestamp(s.start_time / 1e9, UTC),
                    "end_time": datetime.fromtimestamp(s.end_time / 1e9, UTC),
                    "duration_ms": round((s.end_time - s.start_time) / 1e6, 3),
                    "status_code": s.status.status_code.name,
                    "attributes": redact({k: _jsonable(v) for k, v in attrs.items()}),
                }
            )
        if not rows:
            return SpanExportResult.SUCCESS
        try:
            with self._engine.begin() as conn:
                conn.execute(pg_insert(SpanRecord).values(rows).on_conflict_do_nothing(index_elements=["span_id"]))
        except Exception as exc:  # exporter must never break the application
            log.warning("span export to database failed", error=str(exc)[:300])
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._engine.dispose()


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


def setup_tracing(
    service_name: str,
    *,
    otlp_endpoint: str | None,
    database_url: str | None,
    extra_exporter: SpanExporter | None = None,
) -> TracerProvider:
    """Idempotent per process; additional exporters are attached on later calls."""
    global _provider
    with _lock:
        if _provider is None:
            _provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            trace.set_tracer_provider(_provider)
        if otlp_endpoint and f"otlp:{otlp_endpoint}" not in _configured_exporters:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            _provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
            )
            _configured_exporters.add(f"otlp:{otlp_endpoint}")
        if database_url and f"db:{database_url}" not in _configured_exporters:
            _provider.add_span_processor(BatchSpanProcessor(DbSpanExporter(database_url), schedule_delay_millis=1000))
            _configured_exporters.add(f"db:{database_url}")
        if extra_exporter is not None:
            _provider.add_span_processor(BatchSpanProcessor(extra_exporter))
        return _provider


def flush_spans(timeout_ms: int = 5000) -> None:
    if _provider is not None:
        _provider.force_flush(timeout_ms)


tracer = trace.get_tracer("agentos")


@contextmanager
def start_span(
    name: str,
    *,
    task_id: str | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
    context: otel_context.Context | None = None,
) -> Iterator[Span]:
    attrs = {k: _jsonable(v) for k, v in (attributes or {}).items() if v is not None}
    if task_id:
        attrs[TASK_ATTR] = task_id
    with tracer.start_as_current_span(
        name, kind=kind, attributes=attrs, context=context, record_exception=True
    ) as span:
        try:
            yield span
        except BaseException as exc:
            # LangGraph signals a human-approval pause with an exception; that is not an error.
            if type(exc).__name__ not in ("GraphInterrupt", "NodeInterrupt", "ParentCommand"):
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> otel_context.Context | None:
    if not traceparent:
        return None
    return propagate.extract({"traceparent": traceparent})


def trace_id_of(traceparent: str | None) -> str | None:
    if not traceparent:
        return None
    parts = traceparent.split("-")
    return parts[1] if len(parts) == 4 else None

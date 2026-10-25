"""OpenTelemetry W3C spans with a bounded, redacted SQL exporter for local demos."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import delete, insert, select

from app.infrastructure.database import get_engine, platform
from app.security.guardrails import GuardrailEngine

_current = ContextVar("platform_telemetry", default=None)
TABLE = platform["telemetry_spans"]
SAFE_ATTRIBUTES = {
    "component",
    "operation",
    "outcome",
    "error_code",
    "http.method",
    "http.status_code",
    "queue_wait_ms",
    "provider",
    "model",
    "token_count",
    "release_id",
}


class Telemetry:
    def __init__(self, config):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            SimpleSpanProcessor,
            SpanExporter,
            SpanExportResult,
        )
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        engine = get_engine(config.database_url)
        self.engine = engine

        class Exporter(SpanExporter):
            def export(self, spans):
                try:
                    guard = GuardrailEngine()
                    with engine.begin() as conn:
                        for span in spans:
                            attrs = {
                                k: guard.redact(str(v))[:500]
                                for k, v in span.attributes.items()
                                if k in SAFE_ATTRIBUTES
                            }
                            conn.execute(
                                insert(TABLE).values(
                                    span_id=f"{span.context.span_id:016x}",
                                    trace_id=f"{span.context.trace_id:032x}",
                                    parent_id=f"{span.parent.span_id:016x}"
                                    if span.parent
                                    else "",
                                    name=span.name[:80],
                                    status=span.status.status_code.name,
                                    attributes_json=json.dumps(attrs),
                                    started_at=span.start_time / 1e9,
                                    duration_ms=(span.end_time - span.start_time) / 1e6,
                                )
                            )
                    return SpanExportResult.SUCCESS
                except Exception:
                    # Telemetry is best effort, unlike the platform audit ledger.
                    return SpanExportResult.FAILURE

        self.provider = TracerProvider(
            sampler=TraceIdRatioBased(config.telemetry_sample_rate)
        )
        self.provider.add_span_processor(SimpleSpanProcessor(Exporter()))
        self.tracer = self.provider.get_tracer("xc-sx-agent", "27-34")

    @contextmanager
    def span(self, name, *, carrier=None, attributes=None):
        from opentelemetry.trace import Status, StatusCode
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        context = TraceContextTextMapPropagator().extract(carrier) if carrier else None
        token = _current.set(self)
        with self.tracer.start_as_current_span(
            name,
            context=context,
            attributes=attributes or {},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as exc:
                span.set_attribute(
                    "error_code", getattr(exc, "code", type(exc).__name__)
                )
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                _current.reset(token)

    def carrier(self):
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )

        values = {}
        TraceContextTextMapPropagator().inject(values)
        return values

    def traces(self, trace_id):
        with self.engine.connect() as conn:
            return [
                dict(row) | {"attributes": json.loads(row["attributes_json"])}
                for row in conn.execute(
                    select(TABLE)
                    .where(TABLE.c.trace_id == trace_id)
                    .order_by(TABLE.c.started_at)
                    .limit(1000)
                ).mappings()
            ]

    def metrics(self):
        from collections import defaultdict

        from app.platform.evaluation import percentile

        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(TABLE).order_by(TABLE.c.started_at.desc()).limit(10000)
                )
                .mappings()
                .all()
            )
        groups = defaultdict(list)
        for row in rows:
            groups[row["name"]].append(row)
        return {
            name: {
                "count": len(values),
                "errors": sum(x["status"] == "ERROR" for x in values),
                "p95_ms": percentile([x["duration_ms"] for x in values], 0.95),
            }
            for name, values in groups.items()
        }

    def cleanup(self, before):
        with self.engine.begin() as conn:
            return conn.execute(
                delete(TABLE).where(TABLE.c.started_at < before)
            ).rowcount


_lock = threading.Lock()
_instances = {}


def telemetry(config):
    if not config.telemetry_enabled:
        return None
    key = (config.database_url, config.telemetry_sample_rate)
    with _lock:
        if key not in _instances:
            _instances[key] = Telemetry(config)
        return _instances[key]


@contextmanager
def span(name, **attributes):
    current = _current.get()
    if current:
        with current.span(name, attributes=attributes) as active:
            yield active
    else:
        yield None


def carrier():
    current = _current.get()
    return current.carrier() if current else {}


def traced(name):
    """Span an entire transaction, exporting only after its locks are released."""
    from functools import wraps

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def durable_metrics(config):
    from sqlalchemy import func

    engine = get_engine(config.database_url)
    result = {}
    with engine.connect() as conn:
        for name in (
            "ingestion_jobs",
            "connector_jobs",
            "model_calls",
            "conversation_ownership",
        ):
            table = platform[name]
            column = (
                table.c.state if name == "conversation_ownership" else table.c.status
            )
            query = select(column, func.count()).group_by(column)
            if "tenant_id" in table.c:
                query = query.where(table.c.tenant_id == config.tenant_id)
            result[name] = {str(status): count for status, count in conn.execute(query)}
        calls = platform["model_calls"]
        result["tokens"] = {
            "input": conn.scalar(
                select(func.coalesce(func.sum(calls.c.input_tokens), 0)).where(
                    calls.c.tenant_id == config.tenant_id
                )
            ),
            "output": conn.scalar(
                select(func.coalesce(func.sum(calls.c.output_tokens), 0)).where(
                    calls.c.tenant_id == config.tenant_id
                )
            ),
        }
    return result


@contextmanager
def operation_span(config, operation_id, name):
    tracing = telemetry(config) if config else None
    if not tracing:
        yield None
        return
    table = platform["platform_audits"]
    with tracing.engine.connect() as conn:
        raw = conn.scalar(
            select(table.c.detail_json)
            .where(table.c.subject == operation_id, table.c.action == "refund.trace")
            .limit(1)
        )
    with tracing.span(name, carrier=json.loads(raw) if raw else None) as active:
        yield active

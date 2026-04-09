"""
OpenTelemetry setup — vendor-neutral distributed tracing.
Exports to OTLP endpoint when configured; no-op otherwise.
"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from src.config import get_settings


def setup_tracing(app: object) -> None:  # type: ignore[return]
    settings = get_settings()
    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)

    if settings.otel_exporter_otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    else:
        # In production without OTLP: no export. In dev: also no export
        # (ConsoleSpanExporter is excessively verbose — enable manually if debugging traces)
        exporter = None  # type: ignore[assignment]

    if exporter:
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)  # type: ignore[arg-type]


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)

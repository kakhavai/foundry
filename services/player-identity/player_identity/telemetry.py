import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_telemetry(app) -> None:
    resource = Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", "player-identity")}
    )

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    trace.set_tracer_provider(tracer_provider)

    reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(app)
    # instrument_app only patches `app.build_middleware_stack`. Starlette builds
    # and caches that stack on its first `__call__`, which is the lifespan scope
    # — before this function runs — so the patch would never be applied.
    # Rebuilding explicitly is what actually installs OpenTelemetryMiddleware.
    #
    # Without this the failure is silent: the middleware is absent while
    # `_is_instrumented_by_opentelemetry` still reads True, so no server spans
    # are produced and httpx client spans reach Tempo with no parent.
    app.middleware_stack = app.build_middleware_stack()
    HTTPXClientInstrumentor().instrument()

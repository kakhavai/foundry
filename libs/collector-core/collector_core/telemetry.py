"""OTel wiring, once, for the whole collector fleet.

Every collector's `telemetry.py` was the same forty lines differing by exactly
one string -- the `service.name` default. At twenty-six collectors that is
~1,040 lines expressing twenty-six strings.

Line count is the least of it. The reason this file must not be copied is that
its characteristic failure is **silent**. `FastAPIInstrumentor.instrument_app`
only patches `app.build_middleware_stack`; Starlette builds and caches that
stack on its first `__call__`, which *is* the lifespan scope this runs inside,
so the patch arrives too late and `OpenTelemetryMiddleware` is never installed.
Omit the explicit rebuild below and:

- `_is_instrumented_by_opentelemetry` still reads `True`,
- `/metrics` still works,
- the service simply produces no server spans, while its httpx client spans
  arrive in Tempo with no parent.

Nothing fails. Nobody notices until someone goes looking for a trace. Twenty-six
chances to drop that line versus one is the whole argument for this module.

**This module is imported lazily and must stay that way.** `build_collector_app`
resolves `CollectorDescriptor.telemetry_module` -- a dotted *string*, never a
callable -- with `importlib.import_module`, and only once
`OTEL_EXPORTER_OTLP_ENDPOINT` is actually set. Importing it eagerly anywhere
(including from `collector_core/__init__.py`) would defeat the guard that lets
every collector run and test with no collector configured.
"""

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


def resolve_service_name(service_name: str | None = None) -> str:
    """`OTEL_SERVICE_NAME` if set, else the collector's own name.

    Taken from the descriptor rather than a hardcoded literal, which is what
    made this file per-service in the first place. The env var still wins, so
    an operator can rename a service in a values file without a code change.
    """
    return os.getenv("OTEL_SERVICE_NAME") or service_name or "collector"


def setup_telemetry(app, service_name: str | None = None) -> None:
    """Traces to the OTel Collector, metrics to `/metrics`, for one collector.

    `service_name` is the collector's own name; `build_collector_app` passes
    `descriptor.name`. A collector needing more than this writes its own
    `telemetry.py` that calls this first and then adds its specifics -- it does
    not fork the file.
    """
    resource = Resource.create({"service.name": resolve_service_name(service_name)})

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
    # THE line. See the module docstring: without it the middleware is never
    # installed and the failure is completely silent. `test_telemetry.py`
    # guards it by walking the real middleware chain -- asserting
    # `instrument_app` was *called* does not catch this.
    app.middleware_stack = app.build_middleware_stack()
    HTTPXClientInstrumentor().instrument()

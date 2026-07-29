import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from player_projections.main import app


def test_telemetry_not_imported_without_endpoint(monkeypatch):
    """The OTel guard: no endpoint set means telemetry is never even imported."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", "")
    sys.modules.pop("player_projections.telemetry", None)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert "player_projections.telemetry" not in sys.modules


def test_setup_telemetry_called_when_endpoint_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", "")
    calls = []
    monkeypatch.setattr(
        "player_projections.telemetry.setup_telemetry", lambda app: calls.append(app)
    )

    with TestClient(app):
        pass

    assert len(calls) == 1


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patch every global-installing SDK call so tests leave no process state."""
    recorded = {"resource": None, "endpoint": None, "fastapi": 0, "httpx": 0}
    mod = "player_projections.telemetry"

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            recorded["resource"] = kwargs.get("resource")

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr(f"{mod}.TracerProvider", FakeProvider)
    monkeypatch.setattr(f"{mod}.MeterProvider", FakeProvider)
    monkeypatch.setattr(f"{mod}.BatchSpanProcessor", lambda exporter: None)
    monkeypatch.setattr(
        f"{mod}.OTLPSpanExporter",
        lambda endpoint: recorded.__setitem__("endpoint", endpoint),
    )
    monkeypatch.setattr(f"{mod}.PrometheusMetricReader", lambda: None)
    monkeypatch.setattr(f"{mod}.trace.set_tracer_provider", lambda p: None)
    monkeypatch.setattr(f"{mod}.metrics.set_meter_provider", lambda p: None)
    monkeypatch.setattr(
        f"{mod}.FastAPIInstrumentor.instrument_app",
        staticmethod(
            lambda app: recorded.__setitem__("fastapi", recorded["fastapi"] + 1)
        ),
    )

    class FakeHTTPXInstrumentor:
        def instrument(self):
            recorded["httpx"] += 1

    monkeypatch.setattr(f"{mod}.HTTPXClientInstrumentor", FakeHTTPXInstrumentor)
    return recorded


def test_exporter_uses_configured_endpoint(monkeypatch, patched_sdk):
    from player_projections.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["endpoint"] == "http://collector.test:4317"


def test_service_name_defaults_to_player_projections(monkeypatch, patched_sdk):
    from player_projections.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    setup_telemetry(app)

    assert patched_sdk["resource"].attributes["service.name"] == "player-projections"


def test_fastapi_and_httpx_instrumentation_attached(monkeypatch, patched_sdk):
    from player_projections.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["fastapi"] == 1
    assert patched_sdk["httpx"] == 1


@pytest.fixture
def real_fastapi_instrumentation(monkeypatch):
    """Stub every global-installing SDK call EXCEPT the FastAPI instrumentor.

    `patched_sdk` counts `instrument_app` calls, which proves the line is
    reached but not that it achieved anything. This fixture lets the real
    instrumentor run so the middleware chain can be inspected, while still
    leaving no process state behind.
    """
    mod = "player_projections.telemetry"

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr(f"{mod}.TracerProvider", FakeProvider)
    monkeypatch.setattr(f"{mod}.MeterProvider", FakeProvider)
    monkeypatch.setattr(f"{mod}.BatchSpanProcessor", lambda exporter: None)
    monkeypatch.setattr(f"{mod}.OTLPSpanExporter", lambda endpoint: None)
    monkeypatch.setattr(f"{mod}.PrometheusMetricReader", lambda: None)
    monkeypatch.setattr(f"{mod}.trace.set_tracer_provider", lambda p: None)
    monkeypatch.setattr(f"{mod}.metrics.set_meter_provider", lambda p: None)
    # Instrumenting httpx globally would outlive the test and interfere with
    # respx in every other module.
    monkeypatch.setattr(
        f"{mod}.HTTPXClientInstrumentor",
        lambda: SimpleNamespace(instrument=lambda: None),
    )


def test_server_middleware_is_actually_installed(
    monkeypatch, real_fastapi_instrumentation
):
    """The silent failure `patched_sdk` cannot see.

    `instrument_app` only patches `app.build_middleware_stack`. Starlette builds
    and caches that stack on its first `__call__` — the lifespan scope, which
    runs before `setup_telemetry` — so without an explicit rebuild the
    middleware never lands. Nothing raises: `_is_instrumented_by_opentelemetry`
    reads True either way, so the app reports itself instrumented while emitting
    no server spans at all and orphaning every httpx client span in Tempo.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", "")
    original_stack = app.middleware_stack
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

        names, node = [], app.middleware_stack
        while node is not None:
            names.append(type(node).__name__)
            node = getattr(node, "app", None)

        assert any("OpenTelemetry" in name for name in names), (
            "OpenTelemetryMiddleware is missing from the middleware chain — the "
            f"app reports itself instrumented but emits no server spans: {names}"
        )
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        app.middleware_stack = original_stack

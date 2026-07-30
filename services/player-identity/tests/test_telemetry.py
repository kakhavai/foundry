import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from player_identity.main import app


def test_telemetry_not_imported_without_endpoint(monkeypatch):
    """The OTel guard, structural rather than conventional:
    `CollectorDescriptor.telemetry_module` is the dotted string
    `"player_identity.telemetry"`, not a pre-imported callable, so
    `build_collector_app` is the only thing that can ever import it — and
    only inside this env check. Starting from a forced-clean `sys.modules`
    is what makes the negative assertion below trustworthy."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    sys.modules.pop("player_identity.telemetry", None)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert "player_identity.telemetry" not in sys.modules


def test_setup_telemetry_called_when_endpoint_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.delitem(sys.modules, "player_identity.telemetry", raising=False)
    calls = []
    monkeypatch.setattr(
        "player_identity.telemetry.setup_telemetry", lambda app: calls.append(app)
    )

    with TestClient(app):
        pass

    assert len(calls) == 1
    assert "player_identity.telemetry" in sys.modules


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patch every global-installing SDK call so tests leave no process state."""
    recorded = {"resource": None, "endpoint": None, "fastapi": 0, "httpx": 0}

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            recorded["resource"] = kwargs.get("resource")

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr("player_identity.telemetry.TracerProvider", FakeProvider)
    monkeypatch.setattr("player_identity.telemetry.MeterProvider", FakeProvider)
    monkeypatch.setattr(
        "player_identity.telemetry.BatchSpanProcessor", lambda exporter: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.OTLPSpanExporter",
        lambda endpoint: recorded.__setitem__("endpoint", endpoint),
    )
    monkeypatch.setattr(
        "player_identity.telemetry.PrometheusMetricReader", lambda: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.trace.set_tracer_provider", lambda p: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.metrics.set_meter_provider", lambda p: None
    )

    def increment_fastapi(app):
        recorded.__setitem__("fastapi", recorded["fastapi"] + 1)

    monkeypatch.setattr(
        "player_identity.telemetry.FastAPIInstrumentor.instrument_app",
        staticmethod(increment_fastapi),
    )

    class FakeHTTPXInstrumentor:
        def instrument(self):
            recorded["httpx"] += 1

    monkeypatch.setattr(
        "player_identity.telemetry.HTTPXClientInstrumentor", FakeHTTPXInstrumentor
    )
    return recorded


def test_exporter_uses_configured_endpoint(monkeypatch, patched_sdk):
    from player_identity.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["endpoint"] == "http://collector.test:4317"


def test_service_name_from_env(monkeypatch, patched_sdk):
    from player_identity.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "player-identity-canary")
    setup_telemetry(app)

    assert (
        patched_sdk["resource"].attributes["service.name"] == "player-identity-canary"
    )


def test_service_name_defaults_to_the_service(monkeypatch, patched_sdk):
    from player_identity.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    setup_telemetry(app)

    assert patched_sdk["resource"].attributes["service.name"] == "player-identity"


def test_fastapi_and_httpx_instrumentation_attached(monkeypatch, patched_sdk):
    from player_identity.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["fastapi"] == 1
    assert patched_sdk["httpx"] == 1


@pytest.fixture
def real_fastapi_instrumentation(monkeypatch):
    """Stub every global-installing SDK call EXCEPT the FastAPI instrumentor,
    so the real middleware chain can be inspected."""

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr("player_identity.telemetry.TracerProvider", FakeProvider)
    monkeypatch.setattr("player_identity.telemetry.MeterProvider", FakeProvider)
    monkeypatch.setattr(
        "player_identity.telemetry.BatchSpanProcessor", lambda exporter: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.OTLPSpanExporter", lambda endpoint: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.PrometheusMetricReader", lambda: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.trace.set_tracer_provider", lambda p: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.metrics.set_meter_provider", lambda p: None
    )
    monkeypatch.setattr(
        "player_identity.telemetry.HTTPXClientInstrumentor",
        lambda: SimpleNamespace(instrument=lambda: None),
    )


def middleware_chain(application) -> list[str]:
    names, node = [], application.middleware_stack
    while node is not None:
        names.append(type(node).__name__)
        node = getattr(node, "app", None)
    return names


def test_server_middleware_is_actually_installed(
    monkeypatch, real_fastapi_instrumentation
):
    """The silent failure a call-count assertion cannot see.

    `instrument_app` only patches `app.build_middleware_stack`. Starlette
    builds and caches that stack on its first `__call__` — the lifespan
    scope, which runs before `setup_telemetry` — so without the explicit
    rebuild the middleware never lands. Nothing raises:
    `_is_instrumented_by_opentelemetry` reads True either way, so the app
    reports itself instrumented while emitting no server spans at all.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    original_stack = app.middleware_stack
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

        chain = middleware_chain(app)
        assert any("OpenTelemetry" in name for name in chain), (
            "OpenTelemetryMiddleware is missing from the middleware chain — the "
            f"app reports itself instrumented but emits no server spans: {chain}"
        )
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        app.middleware_stack = original_stack


def test_auth_middleware_survives_instrumentation(
    monkeypatch, real_fastapi_instrumentation
):
    """Rebuilding the stack must not drop the bearer-token check — including
    on the resolve routes, which are mounted after `build_collector_app`."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    original_stack = app.middleware_stack
    try:
        with TestClient(app) as client:
            assert client.get("/signals").status_code == 401
            assert client.get("/resolve?name=Someone").status_code == 401
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        app.middleware_stack = original_stack

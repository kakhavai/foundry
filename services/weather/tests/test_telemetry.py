import sys

import pytest
from fastapi.testclient import TestClient

from weather.main import app


def test_telemetry_not_imported_without_endpoint(monkeypatch):
    """The OTel guard: no endpoint set means telemetry is never even imported."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    sys.modules.pop("weather.telemetry", None)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert "weather.telemetry" not in sys.modules


def test_setup_telemetry_called_when_endpoint_set(monkeypatch):
    """With an endpoint set, lifespan wires telemetry up."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    calls = []
    monkeypatch.setattr(
        "weather.telemetry.setup_telemetry", lambda app: calls.append(app)
    )

    with TestClient(app):
        pass

    assert len(calls) == 1


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patch every global-installing SDK call so tests leave no process state."""
    recorded = {"resource": None, "endpoint": None, "fastapi": 0, "httpx": 0}

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            recorded["resource"] = kwargs.get("resource")

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr("weather.telemetry.TracerProvider", FakeProvider)
    monkeypatch.setattr("weather.telemetry.MeterProvider", FakeProvider)
    monkeypatch.setattr("weather.telemetry.BatchSpanProcessor", lambda exporter: None)
    monkeypatch.setattr(
        "weather.telemetry.OTLPSpanExporter",
        lambda endpoint: recorded.__setitem__("endpoint", endpoint),
    )
    monkeypatch.setattr("weather.telemetry.PrometheusMetricReader", lambda: None)
    monkeypatch.setattr("weather.telemetry.trace.set_tracer_provider", lambda p: None)
    monkeypatch.setattr("weather.telemetry.metrics.set_meter_provider", lambda p: None)

    def increment_fastapi(app):
        recorded.__setitem__("fastapi", recorded["fastapi"] + 1)

    monkeypatch.setattr(
        "weather.telemetry.FastAPIInstrumentor.instrument_app",
        staticmethod(increment_fastapi),
    )

    class FakeHTTPXInstrumentor:
        def instrument(self):
            recorded["httpx"] += 1

    monkeypatch.setattr(
        "weather.telemetry.HTTPXClientInstrumentor", FakeHTTPXInstrumentor
    )
    return recorded


def test_exporter_uses_configured_endpoint(monkeypatch, patched_sdk):
    """The exporter targets whatever OTEL_EXPORTER_OTLP_ENDPOINT says."""
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["endpoint"] == "http://collector.test:4317"


def test_service_name_from_env(monkeypatch, patched_sdk):
    """OTEL_SERVICE_NAME overrides the default resource service.name."""
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "weather-canary")
    setup_telemetry(app)

    assert patched_sdk["resource"].attributes["service.name"] == "weather-canary"


def test_service_name_defaults_to_weather(monkeypatch, patched_sdk):
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    setup_telemetry(app)

    assert patched_sdk["resource"].attributes["service.name"] == "weather"


def test_fastapi_and_httpx_instrumentation_attached(monkeypatch, patched_sdk):
    """Deleting either instrumentation line is silent today. This catches it."""
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["fastapi"] == 1
    assert patched_sdk["httpx"] == 1

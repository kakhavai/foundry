import sys

import pytest
from fastapi.testclient import TestClient

from player_projections.main import app


def test_telemetry_not_imported_without_endpoint(monkeypatch):
    """The OTel guard: no endpoint set means telemetry is never even imported."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    sys.modules.pop("player_projections.telemetry", None)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert "player_projections.telemetry" not in sys.modules


def test_setup_telemetry_called_when_endpoint_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("PLAYER_DATA_URL", "")
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

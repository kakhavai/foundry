"""weather's telemetry wiring.

The OTel machinery itself -- the exporter, the resource, both instrumentors,
and the middleware rebuild whose omission is silent -- is proved once in
`libs/collector-core/tests/test_telemetry.py`, against a fake collector. This
file asserts only what is genuinely weather's: that *this* app resolves the
shared module under the env guard, and names itself `weather`.

weather used to carry its own forty-line `telemetry.py` identical to every
other collector's but for one string, and a 218-line test file to match.
"""

import sys

from collector_core.app import DEFAULT_TELEMETRY_MODULE
from collector_core.telemetry import resolve_service_name
from fastapi.testclient import TestClient

from weather.capture import COLLECTOR_NAME
from weather.main import app


def test_weather_uses_the_shared_telemetry_module():
    """Not a copy of it. If weather ever needs extras it declares its own
    module that calls the shared `setup_telemetry` first."""
    assert app.state.collector_spec.name == COLLECTOR_NAME
    assert DEFAULT_TELEMETRY_MODULE == "collector_core.telemetry"


def test_telemetry_is_not_imported_without_the_endpoint(monkeypatch):
    """The guard, against the real service. Starting from a forced-clean
    `sys.modules` is what makes the negative assertion trustworthy."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delitem(sys.modules, DEFAULT_TELEMETRY_MODULE, raising=False)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert DEFAULT_TELEMETRY_MODULE not in sys.modules


def test_setup_is_called_with_weathers_own_name(monkeypatch):
    """The one value that used to be a per-file literal is now taken from the
    descriptor, so it cannot drift from the collector's actual name."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    calls = []
    monkeypatch.setattr(
        f"{DEFAULT_TELEMETRY_MODULE}.setup_telemetry",
        lambda application, service_name=None: calls.append(service_name),
    )

    with TestClient(app):
        pass

    assert calls == [COLLECTOR_NAME]
    assert resolve_service_name(calls[0]) == "weather"

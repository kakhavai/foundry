"""Tests for the fleet's shared OTel wiring.

Built against a fake collector, independent of any real service -- the same
property the rest of `libs/collector-core/tests/` holds. Each service keeps a
thin test asserting only that *its* app resolves the shared module under the
env guard and names itself correctly.

Two things here are worth more than the rest put together:

- `test_server_middleware_is_actually_installed` walks the REAL middleware
  chain. Asserting `instrument_app` was called proves the line was reached,
  not that it achieved anything -- and the failure it misses is completely
  silent.
- `test_telemetry_is_not_imported_without_the_endpoint` starts from a forced-
  clean `sys.modules`, so the negative assertion cannot pass merely because
  nothing happened to import the module yet.
"""

import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from collector_core.app import (
    DEFAULT_TELEMETRY_MODULE,
    CollectorDescriptor,
    build_collector_app,
)
from collector_core.cadence import CadenceClass
from collector_core.metrics import CollectorMetrics

MODULE = "collector_core.telemetry"

# `setup_telemetry` and `resolve_service_name` are deliberately NOT imported at
# this file's top level. Several tests below delete `collector_core.telemetry`
# from `sys.modules` to prove the lazy-import guard, and anything bound here
# would then close over a stale module object while `monkeypatch.setattr`
# patched a freshly re-imported one -- the patches would silently do nothing
# and the real OTLP exporter would run. Each test imports what it needs.


async def _capture(season, week, *, client, lake, now, deadline=None):
    return {}


def make_app(**overrides):
    base = dict(
        name="fake",
        cadence_class=CadenceClass.VOLATILE,
        signal_types=("alpha",),
        supported_filters=("season", "week", "signal_type"),
        capture=_capture,
        signal_matches=lambda row, params: True,
        metrics=CollectorMetrics("telemetry-fake"),
    )
    base.update(overrides)
    return build_collector_app(CollectorDescriptor(**base))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SERVICE_NAME", "COLLECTOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)


# --- the env guard, which consolidation must not weaken ----------------------


def test_the_shared_module_is_the_default_but_is_still_a_dotted_string():
    """A callable default would let the SDK be imported at `app.py`'s own top
    level -- legal Python, every test green, guard silently gone."""
    assert isinstance(DEFAULT_TELEMETRY_MODULE, str)
    assert DEFAULT_TELEMETRY_MODULE == MODULE
    assert CollectorDescriptor.telemetry_module == MODULE


def test_telemetry_is_not_imported_without_the_endpoint(monkeypatch):
    """The whole point of the guard: a collector runs and tests cleanly with
    no OTel collector configured, and never pays the SDK import."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delitem(sys.modules, MODULE, raising=False)

    with TestClient(make_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert MODULE not in sys.modules


def test_collector_core_does_not_import_telemetry_eagerly():
    """Importing the package must not drag in the SDK either -- a lazy
    `telemetry_module` is defeated by an eager `__init__`.

    Run in a subprocess because it is the only honest way to ask "what does a
    fresh interpreter import?". Deleting entries from this process's
    `sys.modules` would leave the already-imported parents behind and prove
    nothing.
    """
    script = textwrap.dedent(
        """
        import sys
        import collector_core
        import collector_core.app
        assert "collector_core.telemetry" not in sys.modules, (
            "collector_core.telemetry was imported eagerly -- the "
            "OTEL_EXPORTER_OTLP_ENDPOINT guard is defeated"
        )
        # The SDK itself must be absent too, not merely the wrapper module.
        assert "opentelemetry.sdk.trace" not in sys.modules
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_telemetry_is_imported_and_called_when_the_endpoint_is_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.delitem(sys.modules, MODULE, raising=False)
    calls = []
    monkeypatch.setattr(
        f"{MODULE}.setup_telemetry",
        lambda app, service_name=None: calls.append((app, service_name)),
    )

    with TestClient(make_app()):
        pass

    assert len(calls) == 1
    assert MODULE in sys.modules


def test_the_collector_name_is_passed_through_from_the_descriptor(monkeypatch):
    """The one line that used to differ per service is now data, taken from
    the descriptor rather than a literal in a copied file."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    calls = []
    monkeypatch.setattr(
        f"{MODULE}.setup_telemetry",
        lambda app, service_name=None: calls.append(service_name),
    )

    with TestClient(make_app(name="betting-lines")):
        pass

    assert calls == ["betting-lines"]


def test_a_collector_can_still_opt_out_entirely(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.delitem(sys.modules, MODULE, raising=False)

    with TestClient(make_app(telemetry_module=None)) as client:
        assert client.get("/health").status_code == 200

    assert MODULE not in sys.modules


def test_a_collector_can_still_declare_its_own_module(monkeypatch):
    """A collector with genuine extras overrides the default; it is not
    forced onto the shared module."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    calls = []
    module = SimpleNamespace(
        setup_telemetry=lambda app, service_name=None: calls.append(service_name)
    )
    monkeypatch.setitem(sys.modules, "fake_collector_own_telemetry", module)

    with TestClient(make_app(telemetry_module="fake_collector_own_telemetry")):
        pass

    assert calls == ["fake"]


# --- the service name --------------------------------------------------------


def test_the_service_name_defaults_to_the_collectors_own_name():
    from collector_core.telemetry import resolve_service_name

    assert resolve_service_name("roster-scope") == "roster-scope"


def test_otel_service_name_overrides_the_collector_name(monkeypatch):
    """An operator must be able to rename a service in a values file without
    a code change -- e.g. to run a canary alongside the real one."""
    from collector_core.telemetry import resolve_service_name

    monkeypatch.setenv("OTEL_SERVICE_NAME", "weather-canary")
    assert resolve_service_name("weather") == "weather-canary"


def test_an_empty_otel_service_name_does_not_win(monkeypatch):
    """An unset ConfigMap value arrives as the empty string, not as absent.
    Letting that through would name every service `""` in Tempo."""
    from collector_core.telemetry import resolve_service_name

    monkeypatch.setenv("OTEL_SERVICE_NAME", "")
    assert resolve_service_name("weather") == "weather"


# --- the SDK calls -----------------------------------------------------------


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patch every global-installing SDK call so tests leave no process state."""
    recorded = {"resource": None, "endpoint": None, "fastapi": 0, "httpx": 0}

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            recorded["resource"] = kwargs.get("resource")

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr(f"{MODULE}.TracerProvider", FakeProvider)
    monkeypatch.setattr(f"{MODULE}.MeterProvider", FakeProvider)
    monkeypatch.setattr(f"{MODULE}.BatchSpanProcessor", lambda exporter: None)
    monkeypatch.setattr(
        f"{MODULE}.OTLPSpanExporter",
        lambda endpoint: recorded.__setitem__("endpoint", endpoint),
    )
    monkeypatch.setattr(f"{MODULE}.PrometheusMetricReader", lambda: None)
    monkeypatch.setattr(f"{MODULE}.trace.set_tracer_provider", lambda p: None)
    monkeypatch.setattr(f"{MODULE}.metrics.set_meter_provider", lambda p: None)

    def increment_fastapi(app):
        recorded["fastapi"] += 1

    monkeypatch.setattr(
        f"{MODULE}.FastAPIInstrumentor.instrument_app", staticmethod(increment_fastapi)
    )

    class FakeHTTPXInstrumentor:
        def instrument(self):
            recorded["httpx"] += 1

    monkeypatch.setattr(f"{MODULE}.HTTPXClientInstrumentor", FakeHTTPXInstrumentor)
    return recorded


def test_the_exporter_targets_the_configured_endpoint(monkeypatch, patched_sdk):
    from collector_core.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(make_app(), "fake")

    assert patched_sdk["endpoint"] == "http://collector.test:4317"


def test_the_resource_carries_the_resolved_service_name(monkeypatch, patched_sdk):
    from collector_core.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(make_app(), "injury-report")

    assert patched_sdk["resource"].attributes["service.name"] == "injury-report"


def test_both_instrumentations_are_attached(monkeypatch, patched_sdk):
    """Deleting either line is silent today. This catches it."""
    from collector_core.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(make_app(), "fake")

    assert patched_sdk["fastapi"] == 1
    assert patched_sdk["httpx"] == 1


# --- the silent failure ------------------------------------------------------


@pytest.fixture
def real_fastapi_instrumentation(monkeypatch):
    """Stub every global-installing SDK call EXCEPT the FastAPI instrumentor.

    `patched_sdk` counts `instrument_app` calls, which proves the line is
    reached but not that it achieved anything. This lets the real instrumentor
    run so the middleware chain can be inspected, while still leaving no
    process state behind.
    """

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr(f"{MODULE}.TracerProvider", FakeProvider)
    monkeypatch.setattr(f"{MODULE}.MeterProvider", FakeProvider)
    monkeypatch.setattr(f"{MODULE}.BatchSpanProcessor", lambda exporter: None)
    monkeypatch.setattr(f"{MODULE}.OTLPSpanExporter", lambda endpoint: None)
    monkeypatch.setattr(f"{MODULE}.PrometheusMetricReader", lambda: None)
    monkeypatch.setattr(f"{MODULE}.trace.set_tracer_provider", lambda p: None)
    monkeypatch.setattr(f"{MODULE}.metrics.set_meter_provider", lambda p: None)
    # Instrumenting httpx globally would outlive the test and interfere with
    # respx everywhere else.
    monkeypatch.setattr(
        f"{MODULE}.HTTPXClientInstrumentor",
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
    """THE test this module exists to hold once instead of twenty-six times.

    `instrument_app` only patches `app.build_middleware_stack`. Starlette
    builds and caches that stack on its first `__call__` -- the lifespan
    scope, which is where `setup_telemetry` runs -- so without an explicit
    rebuild the middleware never lands. Nothing raises:
    `_is_instrumented_by_opentelemetry` reads True either way, so the app
    reports itself instrumented while emitting no server spans at all and
    orphaning every httpx client span in Tempo.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    app = make_app()
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


def test_the_bearer_middleware_survives_instrumentation(
    monkeypatch, real_fastapi_instrumentation
):
    """Rebuilding the stack replaces the cached chain wholesale, so a mistake
    here would silently unauthenticate every collector route in exactly the
    deployments that have telemetry switched on -- production, and nowhere
    else."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.setenv("COLLECTOR_TOKEN", "s3cr3t")
    app = make_app()
    try:
        with TestClient(app) as client:
            assert client.get("/signals").status_code == 401
            authed = client.get("/signals", headers={"Authorization": "Bearer s3cr3t"})
            assert authed.status_code == 200
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

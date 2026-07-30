"""Tests for `build_collector_app`: the process wiring every collector used
to assemble by hand -- environment parsing, `CaptureState`/`RefreshGate`/lake
construction, the lifespan's capture-loop start/stop, auth, and the standard
five routes.

Built against a fake descriptor, independent of weather --
`services/weather/tests/` proves the same wiring end-to-end against the real
service. `libs/collector-core/tests/test_collector_routes.py` and
`test_scheduler.py` already prove the router and the loop's own behaviour;
these tests prove `build_collector_app` assembles them correctly, not that
they work in isolation.
"""

import asyncio
import sys
import threading
import time
import types
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from collector_core.app import CollectorDescriptor, build_collector_app
from collector_core.cadence import CadenceClass
from collector_core.metrics import CollectorMetrics
from collector_core.routes import CaptureState, _default_client_factory, _run_capture

SIGNAL_TYPES = ("alpha",)
SUPPORTED_FILTERS = ("season", "week", "signal_type")
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

ENV_VARS = (
    "REFRESH_MIN_INTERVAL_SECONDS",
    "CAPTURE_DEADLINE_SECONDS",
    "CAPTURE_SEASON",
    "CAPTURE_WEEK",
    "CAPTURE_ENABLED",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "COLLECTOR_TOKEN",
    "LAKE_BUCKET",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """None of these should leak between tests -- each test states exactly
    what it needs."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _signal_matches(row: dict, params: dict) -> bool:
    return True


class _StubCapture:
    """Stands in for a real collector's capture function. Never actually
    called by these tests -- they exercise wiring, not a capture pass."""

    async def __call__(self, season, week, *, client, lake, now, deadline=None):
        return {}


def make_descriptor(**overrides) -> CollectorDescriptor:
    base = dict(
        name="fake",
        cadence_class=CadenceClass.VOLATILE,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=_StubCapture(),
        signal_matches=_signal_matches,
        metrics=CollectorMetrics("collector-app-fake"),
    )
    base.update(overrides)
    return CollectorDescriptor(**base)


# --- environment parsing -----------------------------------------------------


def test_env_defaults_match_what_weather_documented_before_the_refactor():
    """FINDING: a silently dropped default would not fail a single test -- it
    would just quietly diverge from what the Helm chart configures. Pins the
    four env-derived values this function reads."""
    app = build_collector_app(make_descriptor())
    spec = app.state.collector_spec

    assert spec.refresh_gate._min_interval == timedelta(seconds=300)
    assert spec.capture_deadline == timedelta(seconds=300)
    assert spec.default_scope == {"season": 2026, "week": 1}


def test_env_vars_override_the_defaults(monkeypatch):
    monkeypatch.setenv("REFRESH_MIN_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("CAPTURE_DEADLINE_SECONDS", "45")
    monkeypatch.setenv("CAPTURE_SEASON", "2030")
    monkeypatch.setenv("CAPTURE_WEEK", "9")

    spec = build_collector_app(make_descriptor()).state.collector_spec

    assert spec.refresh_gate._min_interval == timedelta(seconds=60)
    assert spec.capture_deadline == timedelta(seconds=45)
    assert spec.default_scope == {"season": 2030, "week": 9}


# --- CollectorSpec / app.state.collector_spec --------------------------------


def test_collector_spec_carries_the_descriptors_own_values():
    descriptor = make_descriptor()
    spec = build_collector_app(descriptor).state.collector_spec

    assert spec.name == "fake"
    assert spec.cadence_class is CadenceClass.VOLATILE
    assert spec.signal_types == SIGNAL_TYPES
    assert spec.supported_filters == SUPPORTED_FILTERS
    assert spec.capture is descriptor.capture
    assert spec.signal_matches is descriptor.signal_matches
    assert spec.metrics is descriptor.metrics


def test_client_factory_defaults_to_the_specs_own_default_when_not_overridden():
    """A collector that does not care about its transport must get exactly
    today's behaviour -- not a second, silently different default."""
    spec = build_collector_app(make_descriptor()).state.collector_spec
    assert spec.client_factory is _default_client_factory


def test_client_factory_override_is_passed_through():
    def factory():
        raise NotImplementedError  # never called by this test

    descriptor = make_descriptor(client_factory=factory)
    spec = build_collector_app(descriptor).state.collector_spec
    assert spec.client_factory is factory


# --- the standard five routes + auth -----------------------------------------


def test_health_and_metrics_are_reachable_without_a_token():
    app = build_collector_app(make_descriptor())
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert "# HELP" in client.get("/metrics").text


def test_data_routes_require_the_configured_bearer_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", "s3cr3t")
    app = build_collector_app(make_descriptor())
    with TestClient(app) as client:
        assert client.get("/signals").status_code == 401
        authed = client.get("/signals", headers={"Authorization": "Bearer s3cr3t"})
        assert authed.status_code == 200


def test_catalog_reflects_the_descriptor(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", "s3cr3t")
    app = build_collector_app(make_descriptor())
    with TestClient(app, headers={"Authorization": "Bearer s3cr3t"}) as client:
        body = client.get("/catalog").json()
    assert body["collector"] == "fake"
    assert body["cadence_class"] == "volatile"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)


# --- telemetry -----------------------------------------------------------
#
# `telemetry_module` is a dotted string, not a callable, precisely so a
# collector author cannot defeat the "don't import OTel unless it's
# configured" guarantee by importing their telemetry module at their own
# file's top level and handing the already-bound function in -- legal
# Python, every test green, invariant silently gone. These tests exercise
# `importlib.import_module` itself, the same call `build_collector_app`
# makes, not a stand-in for it.


def test_telemetry_module_is_never_imported_without_the_otel_endpoint(monkeypatch):
    """Names a module that does not exist anywhere on the import path. If
    `build_collector_app` ever imported it outside the env guard -- the
    exact regression a plain callable allowed -- this fails loudly with
    `ModuleNotFoundError` instead of merely leaving a call list empty."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = build_collector_app(
        make_descriptor(telemetry_module="collector_core_tests_no_such_module")
    )
    with TestClient(app):
        pass  # must not raise ModuleNotFoundError


def test_telemetry_module_is_imported_and_called_when_the_endpoint_is_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    calls = []
    module_name = "collector_core_tests_fake_telemetry_module"
    fake_module = types.ModuleType(module_name)
    fake_module.setup_telemetry = calls.append
    monkeypatch.setitem(sys.modules, module_name, fake_module)

    app = build_collector_app(make_descriptor(telemetry_module=module_name))
    with TestClient(app):
        pass

    assert calls == [app]


def test_a_missing_telemetry_module_is_a_noop_even_with_the_endpoint_set(monkeypatch):
    """`telemetry_module` is optional -- a collector that has not wired
    telemetry yet must still start cleanly."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    app = build_collector_app(make_descriptor())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# --- the capture loop: start/stop gated on CAPTURE_ENABLED -------------------


def test_capture_loop_does_not_start_when_capture_enabled_is_unset(monkeypatch):
    started = threading.Event()

    async def fake_run_capture_loop(*args, **kwargs):
        started.set()
        await asyncio.sleep(999)

    monkeypatch.setattr("collector_core.app.run_capture_loop", fake_run_capture_loop)

    app = build_collector_app(make_descriptor())
    with TestClient(app):
        time.sleep(0.1)
        assert not started.is_set()


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
def test_capture_loop_starts_for_every_truthy_spelling(monkeypatch, value):
    monkeypatch.setenv("CAPTURE_ENABLED", value)
    started = threading.Event()

    async def fake_run_capture_loop(*args, **kwargs):
        started.set()
        await asyncio.sleep(999)

    monkeypatch.setattr("collector_core.app.run_capture_loop", fake_run_capture_loop)

    app = build_collector_app(make_descriptor())
    with TestClient(app):
        assert started.wait(timeout=2.0)


def test_capture_loop_receives_the_descriptors_wiring(monkeypatch):
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    monkeypatch.setenv("CAPTURE_SEASON", "2030")
    monkeypatch.setenv("CAPTURE_WEEK", "9")
    started = threading.Event()
    observed = {}

    async def fake_run_capture_loop(
        state,
        *,
        capture,
        lake,
        season,
        week,
        cadence_class,
        next_event_at,
        metrics,
        capture_deadline,
        client_factory,
        **_,
    ):
        observed.update(
            capture=capture,
            season=season,
            week=week,
            cadence_class=cadence_class,
            metrics=metrics,
            next_event_at=next_event_at,
            capture_deadline=capture_deadline,
        )
        started.set()
        await asyncio.sleep(999)

    monkeypatch.setattr("collector_core.app.run_capture_loop", fake_run_capture_loop)

    descriptor = make_descriptor()
    app = build_collector_app(descriptor)
    with TestClient(app):
        assert started.wait(timeout=2.0)

    assert observed["capture"] is descriptor.capture
    assert observed["season"] == 2030
    assert observed["week"] == 9
    assert observed["cadence_class"] is CadenceClass.VOLATILE
    assert observed["metrics"] is descriptor.metrics
    assert observed["capture_deadline"] == timedelta(seconds=300)
    # No next_event_at was declared -- the loop must still receive a callable
    # rather than None, and that callable must never escalate.
    assert observed["next_event_at"](CaptureState(), NOW) is None


def test_a_declared_next_event_at_is_passed_through_unchanged(monkeypatch):
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    started = threading.Event()
    observed = {}

    def weeks_own_next_event_at(state, now):
        return now + timedelta(hours=3)

    async def fake_run_capture_loop(state, *, next_event_at, **_):
        observed["next_event_at"] = next_event_at
        started.set()
        await asyncio.sleep(999)

    monkeypatch.setattr("collector_core.app.run_capture_loop", fake_run_capture_loop)

    app = build_collector_app(make_descriptor(next_event_at=weeks_own_next_event_at))
    with TestClient(app):
        assert started.wait(timeout=2.0)

    assert observed["next_event_at"] is weeks_own_next_event_at


# --- shutdown -----------------------------------------------------------


async def test_shutdown_cancels_the_background_capture_loop(monkeypatch):
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    started = asyncio.Event()

    async def hanging_loop(*args, **kwargs):
        started.set()
        await asyncio.sleep(999)

    monkeypatch.setattr("collector_core.app.run_capture_loop", hanging_loop)

    app = build_collector_app(make_descriptor())

    before = asyncio.all_tasks()
    async with app.router.lifespan_context(app):
        await started.wait()
        created = asyncio.all_tasks() - before
        assert len(created) == 1
        loop_task = next(iter(created))

    assert loop_task.cancelled()


async def test_shutdown_cancels_a_dispatched_in_flight_capture():
    """A `/refresh` capture still running when the process shuts down must
    not outlive it -- the same guarantee `CaptureState.cancel_in_flight`
    gives every collector."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_capture(season, week, *, client, lake, now, deadline=None):
        started.set()
        await release.wait()
        return {}

    descriptor = make_descriptor(capture=slow_capture)
    app = build_collector_app(descriptor)
    spec = app.state.collector_spec

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(_run_capture(spec, 2026, 1))
        spec.state.in_flight.add(task)
        task.add_done_callback(spec.state.in_flight.discard)
        await started.wait()

    assert task.cancelled()
    assert not spec.state.in_flight

from datetime import UTC, datetime

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import REGISTRY, generate_latest

from weather.main import app


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """Install one real MeterProvider for the whole test session.

    Instruments are created at import time and record nothing until a provider
    exists; anything recorded before it is installed is silently lost. Because
    `set_meter_provider` is one-shot per process, this must happen exactly once,
    before any test records. test_telemetry's `patched_sdk` fixture no-ops
    `set_meter_provider`, so `setup_telemetry` cannot clobber this.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )


@pytest.fixture
def metric_value():
    """Read one series out of real /metrics output.

    Asserting on `generate_latest` rather than the SDK's internal view means
    these tests check exactly what Prometheus scrapes, including OTel's name
    mangling — a rename that broke a chaos query fails here.

    Returns None when the series is absent, which is distinct from a series
    present with value 0.0. Counters accumulate for the whole session, so
    callers assert on the delta across an action, not an absolute value.
    """

    def read(name: str, **labels: str) -> float | None:
        for line in generate_latest(REGISTRY).decode().splitlines():
            if line.startswith("#"):
                continue
            head, _, raw_value = line.rpartition(" ")
            if "{" in head:
                series, _, raw_labels = head.partition("{")
                if series != name:
                    continue
                found = {
                    k: v.strip('"')
                    for k, v in (
                        pair.split("=", 1)
                        for pair in raw_labels.rstrip("}").split(",")
                        if pair
                    )
                }
            else:
                if head != name:
                    continue
                found = {}
            if all(found.get(k) == v for k, v in labels.items()):
                return float(raw_value)
        return None

    return read


TEST_TOKEN = "test-collector-token"


@pytest.fixture(autouse=True)
def _collector_token(monkeypatch):
    """Every route except /health and /metrics needs a token.

    Set one for the whole suite so tests exercise the authenticated path.
    test_auth.py overrides it where it needs a different value.
    """
    monkeypatch.setenv("COLLECTOR_TOKEN", TEST_TOKEN)


@pytest.fixture
def collector_token(_collector_token) -> str:
    return TEST_TOKEN


@pytest.fixture
def client(_collector_token):
    """A TestClient carrying a valid bearer token.

    Tests that need the unauthenticated path build their own client — see
    test_auth.py's `anonymous()`.
    """
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_TOKEN}"}) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_collector_singletons():
    """`app.state.collector_spec.state` and `.refresh_gate` are process-level
    singletons — that's what lets `/signals` serve from a cache and
    `/refresh` enforce an interval floor across requests in production — but
    it makes them a shared-state hazard between tests unless something
    resets them.

    Autouse (unlike `seeded_state`) because leaving this to opt-in would let
    one test's `/refresh` call leak a populated cache, or an armed interval
    floor, into a completely unrelated test later in the run. Runs before
    `seeded_state` populates its fixture data — pytest instantiates autouse
    fixtures ahead of explicitly-requested ones at the same scope — and after
    it tears that data down, so either way the baseline here is what a test
    with no fixture at all should see: empty.
    """
    spec = app.state.collector_spec
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None
    yield
    spec.state.envelopes = {}
    spec.state.last_capture_at = None
    spec.refresh_gate._last_allowed_at = None


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def make_envelope(signal_type: str, signals: list[dict]) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type=signal_type,
        captured_at=NOW,
        upstream=Upstream("open-meteo", NOW),
        scope={"season": 2026, "week": 1},
        coverage=Coverage(expected=len(signals), present=len(signals), missing=[]),
        errors=[],
        signals=signals,
    )


@pytest.fixture
def seeded_state():
    """State-based route tests pre-populate the cache directly, per the repo's
    existing convention — the routes never call an upstream.

    Not autouse: most of the suite (auth, telemetry, metrics) never touches
    `app.state.collector_spec.state`, and forcing it into every test would
    make the fixture's own teardown a hidden dependency for unrelated tests.
    Tests that need seeded data request it by name.
    """
    state = app.state.collector_spec.state
    state.envelopes = {
        "venue_forecast_kickoff": make_envelope(
            "venue_forecast_kickoff",
            [
                {"game_id": "2026_01_CHI_CAR", "venue_id": "CAR00", "team": "CAR"},
                {"game_id": "2026_01_BUF_HOU", "venue_id": "HOU00", "team": "HOU"},
            ],
        ),
        "venue_conditions_current": make_envelope(
            "venue_conditions_current",
            [{"venue_id": "CAR00", "team": "CAR"}],
        ),
    }
    state.last_capture_at = NOW
    yield
    state.envelopes = {}
    state.last_capture_at = None

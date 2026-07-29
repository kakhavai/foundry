"""Contract tests for the shared five-route collector surface.

Built against a fake two-signal-type collector so these tests prove the
router's own behaviour -- universal filtering, unsupported-filter rejection,
refresh-gate semantics -- independent of any real collector.
`services/weather/tests/test_routes.py` is the working reference this
abstraction was extracted from, and pins the same shapes against the real
service.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collector_core.cadence import CadenceClass
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.metrics import CollectorMetrics
from collector_core.refresh import RefreshGate
from collector_core.routes import CaptureState, CollectorSpec, build_collector_router

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
SIGNAL_TYPES = ("alpha", "beta")
SUPPORTED_FILTERS = ("season", "week", "signal_type", "widget_id")


def make_envelope(
    signal_type: str, signals: list[dict], *, season: int = 2026, week: int = 1
) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="fake",
        signal_type=signal_type,
        captured_at=NOW,
        upstream=Upstream("fake-adapter", NOW),
        scope={"season": season, "week": week},
        coverage=Coverage(expected=len(signals), present=len(signals), missing=[]),
        errors=[],
        signals=signals,
    )


def signal_matches(row: dict, params: dict) -> bool:
    """The fake collector's own row filter, standing in for weather's
    game_id/team predicate: matches on `widget_id` when the caller asks for
    one, otherwise passes every row through."""
    widget_id = params.get("widget_id")
    if widget_id is not None and row.get("widget_id") != widget_id:
        return False
    return True


class _StubCapture:
    """Stands in for a real `capture_week`. `/refresh`'s own job is to gate
    and dispatch a capture, not to run one, so this only needs to prove it
    was invoked with the right scope and to hand back something for the
    state to hold."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(
        self, season: int, week: int, *, client, lake, now: datetime
    ) -> dict[str, Envelope]:
        self.calls.append({"season": season, "week": week, "now": now})
        return {
            "alpha": make_envelope(
                "alpha",
                [{"widget_id": "w1"}, {"widget_id": "w2"}],
                season=season,
                week=week,
            ),
            "beta": make_envelope(
                "beta", [{"widget_id": "w1"}], season=season, week=week
            ),
        }


class _NullLake:
    """A minimal stand-in for the `LakeWriter` protocol. The lake's own
    correctness is `test_lake.py`'s job, not this route's -- these routes
    never read or write it directly, only pass it through to `capture`."""

    def write(self, envelope) -> str:
        return ""

    def list_keys(self, collector, signal_type, season, week) -> list[str]:
        return []

    def read(self, key: str) -> dict:
        raise KeyError(key)


@pytest.fixture
def spec() -> CollectorSpec:
    return CollectorSpec(
        name="fake",
        cadence_class=CadenceClass.VOLATILE,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=_StubCapture(),
        state=CaptureState(),
        lake=_NullLake(),
        metrics=CollectorMetrics("fake"),
        refresh_gate=RefreshGate(timedelta(seconds=300)),
        signal_matches=signal_matches,
    )


@pytest.fixture
def client(spec):
    app = FastAPI()
    app.include_router(build_collector_router(spec))
    with TestClient(app) as c:
        yield c


def test_catalog_reports_the_spec(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "fake"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "volatile"
    assert set(body["signal_types"]) == {"alpha", "beta"}
    assert set(body["filters"]) == set(SUPPORTED_FILTERS)


def test_catalog_last_capture_at_is_null_before_any_capture(client):
    body = client.get("/catalog").json()
    assert body["last_capture_at"] is None
    assert body["coverage"] == {}


def test_catalog_reports_coverage_per_signal_type(client, spec):
    spec.state.envelopes = {
        "alpha": make_envelope("alpha", [{"widget_id": "w1"}]),
    }
    spec.state.last_capture_at = NOW

    body = client.get("/catalog").json()

    assert body["last_capture_at"] == "2026-09-11T12:00:00Z"
    assert body["coverage"] == {"alpha": {"expected": 1, "present": 1, "missing": []}}


def test_signals_returns_all_types_by_default(client, spec):
    spec.state.envelopes = {
        "alpha": make_envelope("alpha", [{"widget_id": "w1"}]),
        "beta": make_envelope("beta", [{"widget_id": "w1"}]),
    }

    body = client.get("/signals").json()

    assert {e["signal_type"] for e in body["envelopes"]} == {"alpha", "beta"}


def test_signals_filters_by_signal_type(client, spec):
    spec.state.envelopes = {
        "alpha": make_envelope("alpha", [{"widget_id": "w1"}]),
        "beta": make_envelope("beta", [{"widget_id": "w1"}]),
    }

    body = client.get("/signals?signal_type=beta").json()

    assert [e["signal_type"] for e in body["envelopes"]] == ["beta"]


def test_unknown_signal_type_is_422_not_empty(client, spec):
    """A client bug should surface rather than look like a quiet week -- the
    precedent player-projections set with pos=FLEX."""
    spec.state.envelopes = {"alpha": make_envelope("alpha", [{"widget_id": "w1"}])}

    assert client.get("/signals?signal_type=nonsense").status_code == 422


def test_unsupported_filter_is_422(client):
    """The fake collector emits no `player_id` -- accepting it silently would
    return everything and look like a match."""
    assert client.get("/signals?player_id=x").status_code == 422


def test_supported_collector_filter_is_delegated_to_the_predicate(client, spec):
    spec.state.envelopes = {
        "alpha": make_envelope("alpha", [{"widget_id": "w1"}, {"widget_id": "w2"}]),
    }

    body = client.get("/signals?widget_id=w2").json()

    alpha = next(e for e in body["envelopes"] if e["signal_type"] == "alpha")
    assert [s["widget_id"] for s in alpha["signals"]] == ["w2"]


def test_season_and_week_filter_against_envelope_scope(client, spec):
    """The fake collector's envelopes are scoped to season 2026 / week 1 -- a
    mismatch on either must exclude the envelope, not fall through and
    return everything."""
    spec.state.envelopes = {
        "alpha": make_envelope("alpha", [{"widget_id": "w1"}], season=2026, week=1),
    }

    assert client.get("/signals?season=2099").json() == {"envelopes": [], "count": 0}
    assert client.get("/signals?week=17").json() == {"envelopes": [], "count": 0}


def test_refresh_returns_202_and_a_refresh_id(client):
    response = client.post("/refresh", json={})

    assert response.status_code == 202
    assert response.json()["refresh_id"]


def test_second_refresh_inside_the_floor_is_429_with_retry_after(client):
    client.post("/refresh", json={})

    response = client.post("/refresh", json={})

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_refresh_updates_state_and_last_capture_at(client, spec):
    assert spec.state.last_capture_at is None

    client.post("/refresh", json={})

    assert spec.state.last_capture_at is not None
    assert set(spec.state.envelopes) == {"alpha", "beta"}


def test_health_returns_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_returns_prometheus_text(client):
    assert "# HELP" in client.get("/metrics").text

"""Contract tests for the shared five-route collector surface.

Built against a fake two-signal-type collector so these tests prove the
router's own behaviour -- universal filtering, unsupported-filter rejection,
refresh-gate semantics -- independent of any real collector.
`services/weather/tests/test_routes.py` is the working reference this
abstraction was extracted from, and pins the same shapes against the real
service.
"""

import asyncio
import logging
import threading
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
        self,
        season: int,
        week: int,
        *,
        client,
        lake,
        now: datetime,
        deadline: datetime | None = None,
    ) -> dict[str, Envelope]:
        self.calls.append(
            {"season": season, "week": week, "now": now, "deadline": deadline}
        )
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


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """Poll `predicate` until it is truthy or `timeout` elapses.

    `/refresh` now dispatches its capture rather than awaiting it, and the
    dispatched task runs on `TestClient`'s own background event loop/thread --
    not the one running this test -- so there is no coroutine to `await`
    directly from here. Polling is the honest way to observe "eventually",
    without assuming exactly how many loop iterations a background task needs.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def test_refresh_updates_state_and_last_capture_at(client, spec):
    """The dispatched capture still has to land -- returning 202 early must
    not drop the work, only defer when it is visible."""
    assert spec.state.last_capture_at is None

    client.post("/refresh", json={})

    assert await _wait_until(lambda: spec.state.last_capture_at is not None)
    assert set(spec.state.envelopes) == {"alpha", "beta"}


async def test_refresh_returns_before_the_capture_completes(client, spec):
    """202 means accepted, not finished. A caller must not block on the
    upstream -- that is what makes /refresh unusable under load."""
    started = threading.Event()
    release = threading.Event()

    async def slow_capture(season, week, *, client, lake, now, deadline=None):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {}

    spec.capture = slow_capture

    response = client.post("/refresh", json={})

    assert response.status_code == 202
    assert response.json()["refresh_id"]
    assert await _wait_until(started.is_set), "the capture should have been dispatched"
    # The response above already landed while `release` was still unset, so
    # the capture cannot have finished yet -- prove it explicitly.
    assert spec.state.last_capture_at is None

    release.set()
    assert await _wait_until(lambda: not spec.state.in_flight)


async def test_refresh_updates_state_once_the_dispatched_capture_finishes(client, spec):
    """The work still has to land -- returning early must not drop it."""
    response = client.post("/refresh", json={})

    assert response.status_code == 202
    assert await _wait_until(lambda: spec.state.last_capture_at is not None)
    assert set(spec.state.envelopes) == {"alpha", "beta"}


async def test_a_failed_dispatched_capture_does_not_escape(client, spec, caplog):
    """An upstream failure inside a dispatched capture must be logged, not
    surfaced as an unhandled task exception."""

    async def failing_capture(season, week, *, client, lake, now, deadline=None):
        raise RuntimeError("upstream exploded")

    spec.capture = failing_capture

    with caplog.at_level(logging.ERROR, logger="collector_core.routes"):
        response = client.post("/refresh", json={})
        assert response.status_code == 202
        assert await _wait_until(lambda: not spec.state.in_flight)

    assert "dispatched capture failed" in caplog.text
    # State from before the failure is untouched -- a failed dispatched
    # capture must not corrupt what /signals is currently serving.
    assert spec.state.last_capture_at is None


async def test_shutdown_cancels_an_in_flight_dispatched_capture(spec):
    """A pending capture must not outlive the app or leak a task."""
    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.sleep(999)

    task = asyncio.create_task(never_finishes())
    spec.state.in_flight.add(task)
    task.add_done_callback(spec.state.in_flight.discard)

    await started.wait()
    await spec.state.cancel_in_flight()

    assert task.cancelled()
    assert not spec.state.in_flight


def test_health_returns_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_returns_prometheus_text(client):
    assert "# HELP" in client.get("/metrics").text

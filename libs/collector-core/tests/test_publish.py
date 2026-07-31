"""Tests for the successful-capture tail: `publish_capture`.

Built against a fake collector -- a spy lake and a spy metrics recorder -- so
these prove the library's own behaviour independent of any real one. `moto` is
deliberately not used: CI prunes `collector-core`'s dev dependencies inside
`services/`, so a `moto` import passes locally and fails only in CI.

Two defects are pinned here, and both failed on the code that preceded this
module:

1. **A lake outage cost the whole capture.** Every collector's capture ended
   with a hand-written `await awrite(...)` loop, so a failed write escaped
   before the envelopes were ever returned. `_run_capture`/`run_capture_loop`
   caught it, `CaptureState` was never updated, and `/signals` served the
   previous capture -- or nothing at all on a first run -- while a perfectly
   good capture sat in a local variable. An **object-store** outage cost
   **availability**, which is the exact inversion of the collector contract.

2. **Nothing in the library ever recorded `capture_failure`.** It was called
   only from per-collector `capture.py` files, so an object-store outage was
   indistinguishable from a quiet cadence on
   `collector_capture_failures_total`.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collector_core.cadence import CadenceClass
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.publish import publish_capture
from collector_core.refresh import RefreshGate
from collector_core.routes import CaptureState, CollectorSpec, build_collector_router
from collector_core.scheduler import run_capture_loop

NOW = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)
SIGNAL_TYPES = ("alpha", "beta")


def make_envelope(signal_type: str, signals: list[dict]) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="fake",
        signal_type=signal_type,
        captured_at=NOW,
        upstream=Upstream("fake-adapter", NOW),
        scope={"season": 2026, "week": 1},
        coverage=Coverage(expected=2, present=len(signals), missing=[]),
        errors=[],
        signals=signals,
    )


def make_envelopes() -> dict[str, Envelope]:
    return {
        "alpha": make_envelope("alpha", [{"widget_id": "w1"}, {"widget_id": "w2"}]),
        "beta": make_envelope("beta", [{"widget_id": "w1"}]),
    }


class _SpyLake:
    """Records what was written, and on which thread.

    `fail_on` names the signal types whose write should raise, so a test can
    script a total outage (both) or a partial one (one).
    """

    def __init__(self, *, fail_on: tuple[str, ...] = ()) -> None:
        self.writes: list[Envelope] = []
        self.threads: list[int] = []
        self._fail_on = fail_on

    def write(self, envelope: Envelope) -> str:
        self.threads.append(threading.get_ident())
        if envelope.signal_type in self._fail_on:
            raise OSError("lake unreachable")
        self.writes.append(envelope)
        return "key"

    def list_keys(self, collector, signal_type, season, week, version="1"):
        return []

    def read(self, key):
        raise KeyError(key)


class _SpyMetrics:
    """Stands in for `CollectorMetrics`. Only the two methods the publish and
    failure paths touch are needed, and counting them is the whole point."""

    def __init__(self) -> None:
        self.coverage_calls: list[tuple[str, float]] = []
        self.failures: list[BaseException] = []

    def coverage(self, signal_type: str, ratio: float) -> None:
        self.coverage_calls.append((signal_type, ratio))

    def capture_failure(self, exc: BaseException, reason: str | None = None) -> None:
        self.failures.append(exc)

    def staleness(self, seconds: float) -> None:  # used by run_capture_loop
        pass


# --- DEFECT 1: a lake outage must not cost the capture -----------------------


async def test_a_lake_outage_still_returns_the_captured_envelopes():
    """THE test this module exists for.

    The capture succeeded. The envelopes are built, correct, and in memory.
    Only the durable copy failed. Losing the whole pass over that is a
    contract inversion: an outage must degrade freshness, never availability.
    """
    lake, metrics = _SpyLake(fail_on=SIGNAL_TYPES), _SpyMetrics()
    built = make_envelopes()

    published = await publish_capture(built, lake=lake, metrics=metrics)

    assert published == built
    assert set(published) == set(SIGNAL_TYPES)
    assert lake.writes == []  # nothing was durably stored...
    assert published["alpha"].signals == [{"widget_id": "w1"}, {"widget_id": "w2"}]


async def test_a_partial_lake_outage_returns_every_envelope():
    """One signal type's write failing must not cost the other's."""
    lake, metrics = _SpyLake(fail_on=("alpha",)), _SpyMetrics()

    published = await publish_capture(make_envelopes(), lake=lake, metrics=metrics)

    assert set(published) == set(SIGNAL_TYPES)
    assert [e.signal_type for e in lake.writes] == ["beta"]
    assert len(lake.writes) == 1


async def test_a_healthy_lake_writes_every_envelope():
    lake, metrics = _SpyLake(), _SpyMetrics()

    published = await publish_capture(make_envelopes(), lake=lake, metrics=metrics)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == 2
    assert set(published) == set(SIGNAL_TYPES)
    assert metrics.failures == []


# --- DEFECT 2: the failure must be visible -----------------------------------


async def test_a_failed_lake_write_increments_the_capture_failure_counter():
    """`injury-report` watched a capture keep serving the last good data
    through an unresolvable MinIO endpoint while `collector_capture_failures_total`
    stayed flat -- an object-store outage read as a quiet cadence. Swallowing
    the exception (above) is only defensible if the failure is loud."""
    lake, metrics = _SpyLake(fail_on=SIGNAL_TYPES), _SpyMetrics()

    await publish_capture(make_envelopes(), lake=lake, metrics=metrics)

    assert len(metrics.failures) == 2
    assert all(isinstance(exc, OSError) for exc in metrics.failures)


async def test_coverage_is_recorded_even_for_an_envelope_that_failed_to_write():
    """An absent Prometheus series and a healthy one are indistinguishable in
    PromQL, so the gauge must not simply stop on a lake outage."""
    lake, metrics = _SpyLake(fail_on=SIGNAL_TYPES), _SpyMetrics()

    await publish_capture(make_envelopes(), lake=lake, metrics=metrics)

    assert len(metrics.coverage_calls) == 2
    assert {signal_type for signal_type, _ in metrics.coverage_calls} == set(
        SIGNAL_TYPES
    )


async def test_fail_capture_records_the_failure_counter_itself():
    """The library owns the counter for a failure that ends a pass. It used to
    be called only from per-collector code -- `weather` in 5 places,
    `roster-scope` in 3, `player-identity` in 3, and nothing at all in the
    library -- which is a convention twenty-six authors have to each
    remember."""
    lake, metrics = _SpyLake(), _SpyMetrics()
    original = httpx.ConnectError("upstream down")

    with pytest.raises(httpx.ConnectError):
        await fail_capture(
            original,
            collector="fake",
            signal_types=SIGNAL_TYPES,
            adapter="fake-adapter",
            now=NOW,
            scope={"season": 2026, "week": 1},
            lake=lake,
            metrics=metrics,
        )

    # Exactly once for the pass -- not once per signal type, which would make
    # the failure rate scale with a collector's signal-type count.
    assert len(metrics.failures) == 1
    assert metrics.failures[0] is original


# --- what the fix must NOT break ---------------------------------------------


async def test_fail_capture_still_re_raises_so_the_cache_survives():
    """`fail_capture` deliberately re-raises: there the *capture* failed, and
    installing `present: 0` envelopes over the last good ones destroys good
    data. That is a different case from 'the capture worked and only its
    archival copy failed', and it must keep its opposite answer."""
    lake, metrics = _SpyLake(), _SpyMetrics()
    original = ValueError("shape moved")

    with pytest.raises(ValueError) as caught:
        await fail_capture(
            original,
            collector="fake",
            signal_types=SIGNAL_TYPES,
            adapter="fake-adapter",
            now=NOW,
            scope={"season": 2026, "week": 1},
            lake=lake,
            metrics=metrics,
        )

    assert caught.value is original


async def test_publish_writes_never_run_on_the_event_loop_thread():
    """boto3 is synchronous. A write on the loop thread blocks every other
    request including `/health`."""
    lake, metrics = _SpyLake(), _SpyMetrics()
    loop_thread = threading.get_ident()

    await publish_capture(make_envelopes(), lake=lake, metrics=metrics)

    assert len(lake.threads) == 2
    assert all(tid != loop_thread for tid in lake.threads)


# --- both dispatch paths, end to end -----------------------------------------
#
# `_run_capture` and `run_capture_loop` have the same body and the same
# problem, and neither can be fixed at its own level: an exception arriving
# there carries no envelopes, because `capture` returns them only on its
# success path. Both are therefore fixed by the same thing -- the capture tail
# above -- and both are pinned here.


def make_capture(metrics):
    """A fake collector's capture, shaped exactly like a real one: build the
    envelopes, then hand them to the shared tail. The `lake` it publishes to is
    the one the caller (`_run_capture` or `run_capture_loop`) hands in, which
    is exactly how a real collector receives it."""

    async def capture(season, week, *, client, lake, now, deadline=None):
        envelopes = {
            signal_type: make_envelope(signal_type, [{"widget_id": "fresh"}])
            for signal_type in SIGNAL_TYPES
        }
        return await publish_capture(envelopes, lake=lake, metrics=metrics)

    return capture


async def _wait_until(predicate, message: str, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate(), message


async def test_a_dispatched_refresh_serves_a_capture_whose_lake_write_failed():
    """The `/refresh` path. Before the fix this left `/signals` empty."""
    lake, metrics = _SpyLake(fail_on=SIGNAL_TYPES), _SpyMetrics()
    spec = CollectorSpec(
        name="fake",
        cadence_class=CadenceClass.VOLATILE,
        signal_types=SIGNAL_TYPES,
        supported_filters=SIGNAL_TYPES,
        capture=make_capture(metrics),
        state=CaptureState(),
        lake=lake,
        metrics=metrics,
        refresh_gate=RefreshGate(timedelta(seconds=300)),
        signal_matches=lambda row, params: True,
        default_scope={"season": 2026, "week": 1},
    )
    app = FastAPI()
    app.include_router(build_collector_router(spec))

    with TestClient(app) as client:
        assert client.post("/refresh", json={}).status_code == 202
        await _wait_until(
            lambda: spec.state.last_capture_at is not None,
            "the dispatched capture never landed in CaptureState",
        )
        body = client.get("/signals").json()

    assert body["count"] == 2
    assert len(body["envelopes"]) == 2
    assert all(e["signals"] == [{"widget_id": "fresh"}] for e in body["envelopes"])
    # ...and the durability failure was loud rather than silent.
    assert len(metrics.failures) == 2


async def test_the_cadence_loop_applies_a_capture_whose_lake_write_failed():
    """The background-loop path -- `run_capture_loop`'s own
    `state.apply_capture` call, which had the identical ordering problem."""
    lake, metrics = _SpyLake(fail_on=SIGNAL_TYPES), _SpyMetrics()
    state = CaptureState()

    class _Stop(Exception):
        pass

    async def sleep_once(seconds: float) -> None:
        raise _Stop

    with pytest.raises(_Stop):
        await run_capture_loop(
            state,
            capture=make_capture(metrics),
            lake=lake,
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=lambda s, n: None,
            metrics=metrics,
            sleep=sleep_once,
        )

    assert state.last_capture_at is not None
    assert set(state.envelopes) == set(SIGNAL_TYPES)
    assert len(state.envelopes) == 2
    assert len(metrics.failures) == 2

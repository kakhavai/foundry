"""Tests for the shared capture loop.

Built against fakes so these prove the loop's own behaviour -- escalation,
survival of a failed capture, staleness recording, and injected time -- independent
of any real collector. `next_event_at` here stands in for a collector's own
lookup (weather's kickoff extraction, or a future collector's own perishable
moment); the loop must not know or care what it does internally.
"""

from datetime import UTC, datetime, timedelta

import pytest

from collector_core.cadence import CadenceClass
from collector_core.routes import CaptureState
from collector_core.scheduler import (
    ESCALATED_INTERVAL,
    interval_for_state,
    run_capture_loop,
)

NOW = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)


class _NullLake:
    def write(self, envelope) -> str:
        return ""

    def list_keys(self, collector, signal_type, season, week) -> list[str]:
        return []

    def read(self, key: str) -> dict:
        raise KeyError(key)


class _CaptureStub:
    """Stands in for a real `capture_week`. Each call pops the next scripted
    outcome -- either a captured envelope dict, or an exception to raise, so
    tests can script a failure on one tick and a recovery on the next."""

    def __init__(self, *outcomes: dict | Exception) -> None:
        self.calls: list[dict] = []
        self._outcomes = list(outcomes)

    async def __call__(self, season, week, *, client, lake, now) -> dict:
        self.calls.append({"season": season, "week": week, "now": now})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _StalenessSpy:
    """A minimal stand-in for `CollectorMetrics` -- the loop only ever calls
    `.staleness`, so that is the only method this fake needs."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def staleness(self, seconds: float) -> None:
        self.calls.append(seconds)


class _StopLoop(Exception):
    """Raised by `_FakeSleep` to escape the loop's `while True` once a test
    has seen enough iterations -- `run_capture_loop` never returns on its
    own, so something has to end it deterministically."""


class _FakeSleep:
    def __init__(self, stop_after: int) -> None:
        self.calls: list[float] = []
        self._stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if len(self.calls) >= self._stop_after:
            raise _StopLoop


class _FakeClock:
    """Returns strictly increasing timestamps, `step` apart, so staleness
    (computed from two clock reads within one iteration) is a known,
    non-zero value instead of always 0."""

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._next = start
        self._step = step

    def __call__(self) -> datetime:
        value = self._next
        self._next += self._step
        return value


def no_event(state: CaptureState, now: datetime) -> datetime | None:
    return None


def event_in_progress(state: CaptureState, now: datetime) -> datetime | None:
    """A game already underway -- a negative delta relative to `now`."""
    return now - timedelta(minutes=40)


def event_soon(state: CaptureState, now: datetime) -> datetime | None:
    return now + timedelta(minutes=45)


def event_far_off(state: CaptureState, now: datetime) -> datetime | None:
    return now + timedelta(hours=8)


# --- interval_for_state -----------------------------------------------------


def test_interval_for_state_falls_back_to_base_with_no_event():
    interval = interval_for_state(
        CaptureState(), NOW, cadence_class=CadenceClass.VOLATILE, next_event_at=no_event
    )
    assert interval == timedelta(minutes=15)


def test_interval_for_state_escalates_inside_the_window():
    interval = interval_for_state(
        CaptureState(),
        NOW,
        cadence_class=CadenceClass.VOLATILE,
        next_event_at=event_soon,
    )
    assert interval == ESCALATED_INTERVAL


def test_interval_for_state_stays_escalated_for_an_event_in_progress():
    """A negative delta -- the event already started -- must keep the dense
    cadence. The window closes when the caller stops supplying an event
    (e.g. a finished game), not at the event's start time."""
    interval = interval_for_state(
        CaptureState(),
        NOW,
        cadence_class=CadenceClass.VOLATILE,
        next_event_at=event_in_progress,
    )
    assert interval == ESCALATED_INTERVAL


def test_interval_for_state_ignores_a_far_off_event():
    interval = interval_for_state(
        CaptureState(),
        NOW,
        cadence_class=CadenceClass.VOLATILE,
        next_event_at=event_far_off,
    )
    assert interval == timedelta(minutes=15)


def test_interval_for_state_honours_custom_escalation_params():
    interval = interval_for_state(
        CaptureState(),
        NOW,
        cadence_class=CadenceClass.VOLATILE,
        next_event_at=lambda state, now: now + timedelta(hours=3),
        escalate_within=timedelta(hours=4),
        escalated_interval=timedelta(minutes=1),
    )
    assert interval == timedelta(minutes=1)


# --- run_capture_loop --------------------------------------------------------


async def test_loop_captures_and_updates_state_each_tick():
    state = CaptureState()
    capture = _CaptureStub({"alpha": "envelope-1"}, {"alpha": "envelope-2"})
    sleep = _FakeSleep(stop_after=2)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=no_event,
            metrics=_StalenessSpy(),
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    assert len(capture.calls) == 2
    assert state.envelopes == {"alpha": "envelope-2"}
    assert state.last_capture_at is not None


async def test_loop_survives_a_failed_capture_and_retries_next_tick():
    """An upstream outage must degrade freshness, never take the loop down --
    the next tick must still run."""
    state = CaptureState()
    capture = _CaptureStub(RuntimeError("upstream is down"), {"alpha": "recovered"})
    sleep = _FakeSleep(stop_after=2)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=no_event,
            metrics=_StalenessSpy(),
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    # Both ticks ran -- the exception on tick 1 did not stop tick 2.
    assert len(capture.calls) == 2
    assert state.envelopes == {"alpha": "recovered"}


async def test_a_failed_first_capture_leaves_state_untouched():
    state = CaptureState()
    capture = _CaptureStub(RuntimeError("boom"))
    sleep = _FakeSleep(stop_after=1)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=no_event,
            metrics=_StalenessSpy(),
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    assert state.envelopes == {}
    assert state.last_capture_at is None


async def test_loop_records_staleness_after_a_successful_capture():
    state = CaptureState()
    capture = _CaptureStub({"alpha": "envelope"})
    sleep = _FakeSleep(stop_after=1)
    metrics = _StalenessSpy()

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=no_event,
            metrics=metrics,
            sleep=sleep,
            clock=_FakeClock(NOW, step=timedelta(seconds=3)),
        )

    assert metrics.calls == [3.0]


async def test_loop_does_not_record_staleness_before_any_successful_capture():
    """A capture that never succeeds must never report a staleness value --
    there is nothing to measure staleness from yet."""
    state = CaptureState()
    capture = _CaptureStub(RuntimeError("boom"))
    sleep = _FakeSleep(stop_after=1)
    metrics = _StalenessSpy()

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=no_event,
            metrics=metrics,
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    assert metrics.calls == []


async def test_loop_sleeps_for_the_escalated_interval_near_an_event():
    state = CaptureState()
    capture = _CaptureStub({"alpha": "envelope"})
    sleep = _FakeSleep(stop_after=1)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=event_soon,
            metrics=_StalenessSpy(),
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    assert sleep.calls == [ESCALATED_INTERVAL.total_seconds()]


async def test_loop_stays_escalated_while_an_event_is_in_progress():
    """A negative delta from `next_event_at` -- the event already started --
    must still produce the escalated sleep interval, not the base one."""
    state = CaptureState()
    capture = _CaptureStub({"alpha": "envelope"})
    sleep = _FakeSleep(stop_after=1)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=event_in_progress,
            metrics=_StalenessSpy(),
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    assert sleep.calls == [ESCALATED_INTERVAL.total_seconds()]


async def test_loop_sleeps_for_the_base_interval_far_from_an_event():
    state = CaptureState()
    capture = _CaptureStub({"alpha": "envelope"})
    sleep = _FakeSleep(stop_after=1)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=event_far_off,
            metrics=_StalenessSpy(),
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    assert sleep.calls == [timedelta(minutes=15).total_seconds()]


async def test_loop_uses_the_real_clock_when_none_is_injected():
    """`clock` defaults to wall-clock time -- only `sleep` is overridden here,
    proving the default itself is wired up and callable."""
    state = CaptureState()
    capture = _CaptureStub({"alpha": "envelope"})
    sleep = _FakeSleep(stop_after=1)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2026,
            week=1,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=no_event,
            metrics=_StalenessSpy(),
            sleep=sleep,
        )

    assert state.last_capture_at is not None


async def test_loop_passes_season_week_and_now_through_to_capture():
    state = CaptureState()
    capture = _CaptureStub({"alpha": "envelope"})
    sleep = _FakeSleep(stop_after=1)

    with pytest.raises(_StopLoop):
        await run_capture_loop(
            state,
            capture=capture,
            lake=_NullLake(),
            season=2099,
            week=7,
            cadence_class=CadenceClass.VOLATILE,
            next_event_at=no_event,
            metrics=_StalenessSpy(),
            sleep=sleep,
            clock=_FakeClock(NOW),
        )

    assert capture.calls == [{"season": 2099, "week": 7, "now": NOW}]

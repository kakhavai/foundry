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
from collector_core.conditional import UpstreamUnchanged
from collector_core.routes import DEFAULT_CAPTURE_DEADLINE, CaptureState
from collector_core.scheduler import (
    ESCALATED_INTERVAL,
    interval_for_state,
    run_capture_loop,
)

NOW = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


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

    async def __call__(self, season, week, *, client, lake, now, deadline=None) -> dict:
        self.calls.append(
            {"season": season, "week": week, "now": now, "deadline": deadline}
        )
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


class _RecordingMetrics:
    """A minimal `CollectorMetrics` stand-in for the 304 tests: records which
    outcome fired (`"unchanged"` vs. `"failure"`) rather than caring about
    label values, and no-ops `staleness` since `run_capture_loop` always
    calls it once per tick regardless of outcome."""

    def __init__(self, recorded: list[str]) -> None:
        self._recorded = recorded

    def staleness(self, seconds: float) -> None:
        pass

    def upstream_unchanged(self) -> None:
        self._recorded.append("unchanged")

    def capture_failure(self, exc: BaseException, reason: str | None = None) -> None:
        self._recorded.append("failure")


async def _run_one_loop_tick(
    state: CaptureState,
    capture,
    *,
    now: datetime,
    metrics: object | None = None,
) -> None:
    """Drive `run_capture_loop` for exactly one iteration with the clock
    fixed at `now` for the whole tick -- so a test can assert the exact
    value `state` ends up with -- reusing the `_FakeSleep`/`_StopLoop`
    fixtures the rest of this file drives the loop with."""
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
            metrics=metrics if metrics is not None else _RecordingMetrics([]),
            sleep=sleep,
            clock=lambda: now,
        )


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


async def test_loop_emits_staleness_since_process_start_before_any_capture_succeeds():
    """FINDING 5 regression: `scheduler.py` used to skip staleness entirely
    until the first successful capture (`if state.last_capture_at is not
    None`) -- so during a total outage `collector_staleness_seconds` never
    existed as a series at all, and an absent series cannot page. It must
    report elapsed-since-process-start instead, so the series exists from
    the very first tick.
    """
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

    assert state.last_capture_at is None
    # _FakeClock advances 1s per call: one read for `now` at the top of the
    # iteration (this becomes `process_started_at`), one more to compute
    # elapsed time after the failed capture -- 1.0s apart.
    assert metrics.calls == [1.0]


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

    assert capture.calls == [
        {
            "season": 2099,
            "week": 7,
            "now": NOW,
            "deadline": NOW + DEFAULT_CAPTURE_DEADLINE,
        }
    ]


async def test_loop_passes_now_plus_capture_deadline_to_capture():
    """The pass-level deadline is derived from the tick's own `now`, not
    wall-clock time read a second time -- so it stays deterministic under a
    fake clock."""
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
            clock=_FakeClock(NOW),
            capture_deadline=timedelta(seconds=90),
        )

    assert capture.calls[0]["deadline"] == NOW + timedelta(seconds=90)


async def test_loop_passes_no_deadline_when_capture_deadline_is_none():
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
            clock=_FakeClock(NOW),
            capture_deadline=None,
        )

    assert capture.calls[0]["deadline"] is None


async def test_loop_opens_its_client_via_the_injected_client_factory():
    """FINDING 7: `run_capture_loop` used to hardcode
    `httpx.AsyncClient(timeout=10.0)` -- the loop performs virtually every
    capture, so a collector's `client_factory` (the knob that lets it own its
    own transport config) silently did nothing on the path that matters most,
    even though `/refresh` (`collector_core.routes._run_capture`) already
    honoured it.
    """
    state = CaptureState()
    opened: list[object] = []

    class _RecordingClient:
        async def __aenter__(self):
            opened.append(self)
            return self

        async def __aexit__(self, *exc_info):
            return False

    def custom_client_factory():
        return _RecordingClient()

    seen_clients: list[object] = []

    async def capture(season, week, *, client, lake, now, deadline=None) -> dict:
        seen_clients.append(client)
        return {"alpha": "envelope"}

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
            client_factory=custom_client_factory,
        )

    assert len(opened) == 1
    assert isinstance(opened[0], _RecordingClient)
    assert seen_clients == [opened[0]]


# --- run_capture_loop: UpstreamUnchanged (a 304 is a healthy pass) ----------


async def test_a_304_does_not_replace_the_last_good_envelopes():
    """The regression this whole mechanism must not cause: an unchanged
    upstream must never cost `/signals` the data it is already serving."""
    state = CaptureState()
    good = {"a_signal": object()}
    state.apply_capture(good, T0)

    async def capture(season, week, **kwargs):
        raise UpstreamUnchanged("http://x/d.csv", source_ref='W/"v1"')

    await _run_one_loop_tick(state, capture, now=T0 + timedelta(minutes=15))

    assert state.envelopes is good


async def test_a_304_advances_last_capture_at():
    """Staleness resets, because the data was confirmed current."""
    state = CaptureState()
    state.apply_capture({"a_signal": object()}, T0)

    async def capture(season, week, **kwargs):
        raise UpstreamUnchanged("http://x/d.csv")

    await _run_one_loop_tick(state, capture, now=T0 + timedelta(minutes=15))

    assert state.last_capture_at == T0 + timedelta(minutes=15)


async def test_a_304_is_counted_as_unchanged_not_as_a_failure():
    state = CaptureState()
    state.apply_capture({"a_signal": object()}, T0)
    recorded: list[str] = []

    async def capture(season, week, **kwargs):
        raise UpstreamUnchanged("http://x/d.csv")

    metrics = _RecordingMetrics(recorded)
    await _run_one_loop_tick(
        state, capture, now=T0 + timedelta(minutes=15), metrics=metrics
    )

    assert "unchanged" in recorded
    assert "failure" not in recorded


async def test_a_real_failure_still_leaves_the_clock_alone():
    """The existing contract, re-asserted: only a 304 is special."""
    state = CaptureState()
    state.apply_capture({"a_signal": object()}, T0)

    async def capture(season, week, **kwargs):
        raise RuntimeError("upstream exploded")

    await _run_one_loop_tick(state, capture, now=T0 + timedelta(minutes=15))

    assert state.last_capture_at == T0

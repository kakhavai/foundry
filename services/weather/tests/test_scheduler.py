"""weather's own scheduling piece: extracting the soonest kickoff worth
watching out of its cached signals, and the escalation interval that
extraction feeds into the shared loop (`collector_core.scheduler`).

The loop itself is exercised by `libs/collector-core/tests/test_scheduler.py`
against fakes; these tests only prove weather's binding -- `next_kickoff` and
the cadence-class-bound `interval_for_state` -- is wired correctly.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import NullLakeWriter

from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL
from weather.capture import CaptureState
from weather.scheduler import interval_for_state, next_kickoff
from weather.scheduler import run_capture_loop as weather_run_capture_loop

NOW = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)

SCHEDULE_HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)
HOME_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)
# 13:00 America/New_York on 2026-09-13 (EDT, UTC-4) is 17:00 UTC. This "now"
# sits eleven hours before kickoff -- well outside the 90-minute escalation
# window -- so the integration test below observes the base interval.
INTEGRATION_NOW = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)


class _StopLoop(Exception):
    """Escapes the loop's `while True` once a test has seen one tick."""


class _FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        raise _StopLoop


def _hourly_payload() -> dict:
    dates = sorted({"2026-09-13", INTEGRATION_NOW.strftime("%Y-%m-%d")})
    times = [f"{d}T{h:02d}:00" for d in dates for h in range(0, 24)]
    n = len(times)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [68.0] * n,
            "apparent_temperature": [67.0] * n,
            "relative_humidity_2m": [62] * n,
            "wind_speed_10m": [11.0] * n,
            "wind_gusts_10m": [18.0] * n,
            "wind_direction_10m": [210] * n,
            "precipitation": [0.0] * n,
            "precipitation_probability": [10] * n,
        }
    }


def _mock_upstreams() -> None:
    schedule_text = "\n".join([SCHEDULE_HEADER, HOME_GAME]) + "\n"
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(200, text=schedule_text))
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=_hourly_payload())
    )


@respx.mock
async def test_run_capture_loop_wires_weathers_capture_cadence_and_metrics_in():
    """End-to-end proof of the binding in `weather/scheduler.py`: one tick of
    weather's own `run_capture_loop` populates the state from a real
    `capture_week` call, using weather's cadence class and kickoff lookup --
    without the caller (`main.py`'s lifespan) supplying any of that itself."""
    _mock_upstreams()
    state = CaptureState()
    sleep = _FakeSleep()

    with pytest.raises(_StopLoop):
        await weather_run_capture_loop(
            state,
            lake=NullLakeWriter(),
            season=2026,
            week=1,
            sleep=sleep,
            clock=lambda: INTEGRATION_NOW,
            # This test injects a fixed calendar date well removed from real
            # wall-clock time to prove the cadence binding, not deadline
            # enforcement -- `capture_week`'s deadline check reads the real
            # clock (see `weather.capture._wall_clock`), so pairing a fake
            # `now` with a live deadline would make this test's outcome
            # depend on how far the real clock has drifted from the fixed
            # date above. Deadline behaviour has its own tests in
            # test_capture.py.
            capture_deadline=None,
        )

    assert set(state.envelopes) == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }
    assert state.last_capture_at == INTEGRATION_NOW
    # The one game kicks off well outside the 90-minute window from NOW, so
    # the loop must have slept for the volatile base interval, not escalated.
    assert sleep.calls == [timedelta(minutes=15).total_seconds()]


@respx.mock
async def test_run_capture_loop_honours_an_injected_client_factory():
    """FINDING 7: the shared loop used to hardcode its own
    `httpx.AsyncClient(timeout=10.0)` regardless of what a collector
    configured, so weather's own `client_factory` knob -- the same one
    `/refresh` (`CollectorSpec.client_factory`) already honoured -- silently
    did nothing on the loop, the path that performs virtually every capture.
    """
    _mock_upstreams()
    state = CaptureState()
    sleep = _FakeSleep()
    opened: list[httpx.AsyncClient] = []

    def custom_client_factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(timeout=5.0)
        opened.append(client)
        return client

    with pytest.raises(_StopLoop):
        await weather_run_capture_loop(
            state,
            lake=NullLakeWriter(),
            season=2026,
            week=1,
            sleep=sleep,
            clock=lambda: INTEGRATION_NOW,
            capture_deadline=None,
            client_factory=custom_client_factory,
        )

    assert len(opened) == 1
    assert opened[0].timeout.connect == 5.0
    assert set(state.envelopes) == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }


def state_with_kickoffs(*kickoffs: datetime) -> CaptureState:
    state = CaptureState()
    state.envelopes = {
        "venue_forecast_kickoff": Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="weather",
            signal_type="venue_forecast_kickoff",
            captured_at=NOW,
            upstream=Upstream("open-meteo", NOW),
            scope={"season": 2026, "week": 1},
            coverage=Coverage(expected=len(kickoffs), present=len(kickoffs)),
            errors=[],
            signals=[
                {
                    "game_id": f"g{i}",
                    "forecast_valid_at": k.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                for i, k in enumerate(kickoffs)
            ],
        )
    }
    return state


def test_next_kickoff_picks_the_soonest_future_game():
    state = state_with_kickoffs(
        NOW + timedelta(hours=5), NOW + timedelta(hours=1), NOW + timedelta(hours=9)
    )
    assert next_kickoff(state, NOW) == NOW + timedelta(hours=1)


def test_next_kickoff_ignores_games_already_finished():
    """A game three hours past kickoff is over; it must not hold the dense
    cadence open forever."""
    state = state_with_kickoffs(NOW - timedelta(hours=5), NOW + timedelta(hours=8))
    assert next_kickoff(state, NOW) == NOW + timedelta(hours=8)


def test_next_kickoff_keeps_a_game_in_progress():
    """Within the game window, current conditions are the densest signal."""
    state = state_with_kickoffs(NOW - timedelta(minutes=40))
    assert next_kickoff(state, NOW) == NOW - timedelta(minutes=40)


def test_next_kickoff_is_none_when_nothing_is_scheduled():
    assert next_kickoff(CaptureState(), NOW) is None


def test_next_kickoff_ignores_signals_missing_forecast_valid_at():
    """A row without a timestamp (e.g. a partially-failed capture) must be
    skipped rather than raising."""
    state = state_with_kickoffs(NOW + timedelta(hours=2))
    state.envelopes["venue_forecast_kickoff"].signals.append({"game_id": "gX"})
    assert next_kickoff(state, NOW) == NOW + timedelta(hours=2)


def test_interval_is_the_volatile_base_far_from_kickoff():
    state = state_with_kickoffs(NOW + timedelta(hours=8))
    assert interval_for_state(state, NOW) == timedelta(minutes=15)


def test_interval_escalates_inside_the_window():
    state = state_with_kickoffs(NOW + timedelta(minutes=45))
    assert interval_for_state(state, NOW) == timedelta(minutes=5)


def test_interval_de_escalates_after_the_window_closes():
    """Back to 15 minutes once the last game is well past."""
    state = state_with_kickoffs(NOW - timedelta(hours=6))
    assert interval_for_state(state, NOW) == timedelta(minutes=15)


def test_interval_falls_back_to_base_with_an_empty_cache():
    assert interval_for_state(CaptureState(), NOW) == timedelta(minutes=15)

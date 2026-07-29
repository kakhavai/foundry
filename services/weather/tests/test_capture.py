from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.lake import NullLakeWriter

from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL
from weather.capture import assert_forecast_hour, capture_week

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)
HOME_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)
MUNICH_GAME = (
    "2026_10_NE_DET,2026,REG,1,2026-09-13,09:30,NE,DET,Neutral,dome,grass,DET00,"
    "FC Bayern Munich Stadium"
)


def schedule_csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def hourly_payload() -> dict:
    # `fetch_current_conditions` (weather/adapters/forecast.py) takes its
    # reference time as the `now` capture_week hands it, truncated to the
    # hour — it no longer reads the wall clock (Task 13, Step 3b). A payload
    # keyed on the fixed test NOW and the fixed kickoff date is therefore
    # deterministic on any calendar date; that determinism is the proof the
    # clock injection landed.
    dates = sorted({"2026-09-13", NOW.strftime("%Y-%m-%d")})
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


def mock_upstreams(schedule_text: str) -> None:
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(200, text=schedule_text))
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=hourly_payload())
    )


@respx.mock
async def test_emits_both_signal_types():
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    assert set(result) == {"venue_forecast_kickoff", "venue_conditions_current"}


@respx.mock
async def test_forecast_coverage_counts_every_game_that_week():
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    coverage = result["venue_forecast_kickoff"].coverage
    assert coverage.expected == 1
    assert coverage.present == 1
    assert coverage.missing == []


@respx.mock
async def test_neutral_site_lands_in_missing_and_emits_no_signal():
    """The whole point: never resolve to the designated home team's venue."""
    mock_upstreams(schedule_csv(HOME_GAME, MUNICH_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.expected == 2
    assert envelope.coverage.present == 1
    assert envelope.coverage.missing == ["2026_10_NE_DET"]
    assert [s["game_id"] for s in envelope.signals] == ["2026_01_CHI_CAR"]
    assert envelope.errors[0]["reason"] == "neutral_site_venue_unknown"


@respx.mock
async def test_a_bye_week_venue_produces_nothing():
    """No game, no record — under either signal type."""
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    venues = {s["venue_id"] for s in result["venue_conditions_current"].signals}
    assert venues == {"CAR00"}


@respx.mock
async def test_two_games_at_one_venue_give_two_forecasts_but_one_current():
    second = HOME_GAME.replace("2026_01_CHI_CAR", "2026_01_ATL_CAR").replace(
        ",CHI,CAR,", ",ATL,CAR,"
    )
    mock_upstreams(schedule_csv(HOME_GAME, second))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    assert len(result["venue_forecast_kickoff"].signals) == 2
    assert len(result["venue_conditions_current"].signals) == 1


@respx.mock
async def test_total_upstream_failure_still_writes_an_envelope():
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=schedule_csv(HOME_GAME))
    )
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.present == 0
    assert envelope.signals == []
    assert envelope.errors, "a failed capture must record why"


@respx.mock
async def test_current_conditions_failure_is_isolated_from_a_healthy_forecast():
    """The forecast fetch (per game) and the current-conditions fetch (per
    venue) are separate upstream calls — one failing must not take the other
    down with it, and must still land in its own coverage/errors."""
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=schedule_csv(HOME_GAME))
    )
    respx.get(FORECAST_URL).mock(
        side_effect=[
            httpx.Response(200, json=hourly_payload()),  # forecast: succeeds
            httpx.Response(503),  # current conditions: fails
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    forecast = result["venue_forecast_kickoff"]
    assert forecast.coverage.present == 1
    assert forecast.signals != []

    current = result["venue_conditions_current"]
    assert current.coverage.present == 0
    assert current.coverage.missing == ["CAR00"]
    assert current.signals == []
    assert current.errors, "a failed current-conditions fetch must record why"


@respx.mock
async def test_capture_raises_when_the_adapter_returns_a_mismatched_hour(
    monkeypatch,
):
    """Guards the `assert_forecast_hour` call inside `capture_week` itself.

    `fetch_forecast_at` always echoes back the requested `valid_at` unchanged,
    so no respx fixture can make the adapter legitimately disagree with
    kickoff — the mismatch has to be injected at the capture layer to prove
    the write-time guard is actually wired in, not just unit-testable in
    isolation.
    """
    mock_upstreams(schedule_csv(HOME_GAME))

    async def _stale_forecast(lat, lon, valid_at, client, lead_hours=None):
        return {
            "forecast_valid_at": valid_at - timedelta(hours=3),
            "temperature_f": 68.0,
            "feels_like_f": 67.0,
            "wind_speed_mph": 11.0,
            "wind_gust_mph": 18.0,
            "wind_direction_deg": 210,
            "precipitation_type": "none",
            "precipitation_probability": 0.1,
            "precipitation_rate_in_hr": 0.0,
            "humidity_pct": 62.0,
            "bands": {
                "temperature_f": {"p10": 61.0, "p50": 68.0, "p90": 75.0},
                "wind_speed_mph": {"p10": 8.0, "p50": 11.0, "p90": 14.0},
                "precipitation_rate_in_hr": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
            },
        }

    monkeypatch.setattr("weather.capture.fetch_forecast_at", _stale_forecast)

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="forecast_valid_at"):
            await capture_week(2026, 1, client=client, lake=NullLakeWriter(), now=NOW)


class SpyLakeWriter:
    """Records what it was handed. Deliberately NOT moto/S3.

    `collector-core` already tests real object-store semantics against moto,
    and its dev dependencies are not installed for this service in CI — a moto
    import here passes locally off a shared virtualenv and fails in CI, which is
    the worst place to find out. What weather needs to prove is narrower anyway:
    that a capture hands one envelope per signal type to whatever writer it was
    given. The storage layer's correctness is not this service's test to own.
    """

    def __init__(self) -> None:
        self.written: list = []

    def write(self, envelope) -> str:
        self.written.append(envelope)
        return f"spy://{envelope.signal_type}"

    def list_keys(self, collector, signal_type, season, week) -> list[str]:
        return [f"spy://{e.signal_type}" for e in self.written]

    def read(self, key: str) -> dict:
        raise KeyError(key)


@respx.mock
async def test_capture_writes_one_envelope_per_signal_type_to_the_lake():
    lake = SpyLakeWriter()
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        await capture_week(2026, 1, client=client, lake=lake, now=NOW)

    assert {e.signal_type for e in lake.written} == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }
    assert len(lake.written) == 2


def test_forecast_hour_assertion_accepts_the_matching_hour():
    kickoff = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
    assert_forecast_hour(kickoff, kickoff)


def test_forecast_hour_assertion_truncates_to_the_hour():
    kickoff = datetime(2026, 9, 13, 17, 25, tzinfo=UTC)
    assert_forecast_hour(datetime(2026, 9, 13, 17, 0, tzinfo=UTC), kickoff)


def test_forecast_hour_assertion_rejects_a_different_hour():
    """This is the write-time guard against a republished nowcast."""
    kickoff = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="forecast_valid_at"):
        assert_forecast_hour(kickoff - timedelta(hours=3), kickoff)

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.lake import NullLakeWriter, lake_key

from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL
from weather.capture import (
    REGULAR_SEASON_GAMES_FLOOR,
    REGULAR_SEASON_WEEKS,
    SIGNAL_TYPES,
    assert_forecast_hour,
    capture_week,
    games_floor,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)
HOME_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)
SECOND_HOME_GAME = (
    "2026_01_NYG_DET,2026,REG,1,2026-09-13,13:00,NYG,DET,Home,dome,turf,DET00,"
    "Ford Field"
)
MUNICH_GAME = (
    "2026_10_NE_DET,2026,REG,1,2026-09-13,09:30,NE,DET,Neutral,dome,grass,DET00,"
    "FC Bayern Munich Stadium"
)


def schedule_csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


# The scope tests use when they are asserting exact counts rather than the
# floor. `games_floor` floors a *regular-season* week at 13 (32 teams, at most
# six on bye), which is right in production and swamps a one- or two-row
# fixture. Postseason weeks floor to 1, so these tests can pin the accounting
# without switching off the floor they are not about -- and the floor gets its
# own tests below rather than being incidentally asserted here.
POSTSEASON_WEEK = 19


def postseason_csv(*rows: str) -> str:
    return schedule_csv(*(row.replace(",REG,1,", ",POST,19,") for row in rows))


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
    mock_upstreams(postseason_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, POSTSEASON_WEEK, client=client, lake=NullLakeWriter(), now=NOW
        )
    coverage = result["venue_forecast_kickoff"].coverage
    assert coverage.expected == 1
    assert coverage.present == 1
    assert coverage.missing == []


@respx.mock
async def test_neutral_site_lands_in_missing_and_emits_no_signal():
    """The whole point: never resolve to the designated home team's venue."""
    mock_upstreams(postseason_csv(HOME_GAME, MUNICH_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, POSTSEASON_WEEK, client=client, lake=NullLakeWriter(), now=NOW
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
async def test_a_total_forecast_outage_does_not_zero_current_conditions_expected():
    """FINDING 1: `resolved` (the current-conditions expected set) used to be
    populated only after a forecast succeeded, so a total forecast outage --
    e.g. every kickoff beyond the provider's ~16-day model horizon -- drove
    `venue_conditions_current` coverage to `expected: 0, present: 0`, which
    `Coverage.ratio` reads as a healthy 1.0 rather than the missing signal it
    actually is. A venue hosting a game must count toward current-conditions
    `expected` regardless of whether that game's forecast succeeds -- venue
    resolution and forecast fetching are separate concerns.
    """
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(
            200, text=postseason_csv(HOME_GAME, SECOND_HOME_GAME)
        )
    )
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, POSTSEASON_WEEK, client=client, lake=NullLakeWriter(), now=NOW
        )

    forecast = result["venue_forecast_kickoff"]
    assert forecast.coverage.present == 0
    assert forecast.signals == []

    current = result["venue_conditions_current"]
    assert current.coverage.expected == 2  # two distinct venues host a game
    assert current.coverage.present == 0
    assert current.coverage.ratio == 0.0  # not 1.0 -- this is a real outage
    assert current.signals == []


class _ExplodingLakeWriter:
    """Every write raises -- stands in for a bucket that is unreachable or
    whose credentials never arrived (both `optional: true` in the Helm
    values, so the pod starts fine and looks Healthy)."""

    def write(self, envelope) -> str:
        raise RuntimeError("lake unreachable")

    def list_keys(self, collector, signal_type, season, week) -> list[str]:
        return []

    def read(self, key: str) -> dict:
        raise KeyError(key)


@respx.mock
async def test_a_lake_write_failure_is_recorded_and_still_propagates(metric_value):
    """FINDING 5: `lake.write` used to sit outside every try/except -- a
    total lake outage propagated to the caller (logged and swallowed)
    without incrementing a single counter, one of the two most likely
    total-outage modes (the other is the schedule feed itself, covered in
    `test_failure_metrics.py`).
    """
    mock_upstreams(schedule_csv(HOME_GAME))

    before = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="unknown",
        )
        or 0.0
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="lake unreachable"):
            await capture_week(
                2026, 1, client=client, lake=_ExplodingLakeWriter(), now=NOW
            )
    after = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="unknown",
        )
        or 0.0
    )

    assert after - before == 1.0


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
async def test_a_pass_stops_at_the_deadline_and_records_the_remainder(monkeypatch):
    """Truncation is a fact to record, not a silent short week."""
    mock_upstreams(schedule_csv(HOME_GAME, SECOND_HOME_GAME))

    deadline = NOW + timedelta(seconds=300)
    calls = {"n": 0}

    def fake_wall_clock() -> datetime:
        calls["n"] += 1
        # The first game's deadline check passes; every check after that is
        # past the deadline, so the second game is truncated.
        if calls["n"] <= 1:
            return deadline - timedelta(seconds=1)
        return deadline + timedelta(seconds=1)

    monkeypatch.setattr("weather.capture._wall_clock", fake_wall_clock)

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW, deadline=deadline
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.present < envelope.coverage.expected
    assert envelope.coverage.missing  # the games never attempted
    assert any(e["reason"] == "deadline_exceeded" for e in envelope.errors)


@respx.mock
async def test_signals_captured_before_the_deadline_are_kept(monkeypatch):
    """A partial capture is worth more than none -- do not discard it."""
    mock_upstreams(schedule_csv(HOME_GAME, SECOND_HOME_GAME))

    deadline = NOW + timedelta(seconds=300)
    calls = {"n": 0}

    def fake_wall_clock() -> datetime:
        calls["n"] += 1
        if calls["n"] <= 1:
            return deadline - timedelta(seconds=1)
        return deadline + timedelta(seconds=1)

    monkeypatch.setattr("weather.capture._wall_clock", fake_wall_clock)

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW, deadline=deadline
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.present == 1
    assert [s["game_id"] for s in envelope.signals] == ["2026_01_CHI_CAR"]


@respx.mock
async def test_a_pass_stops_at_the_deadline_during_current_conditions(monkeypatch):
    """The current-conditions loop enforces the same deadline as the
    forecast loop -- both games' forecasts land before the venue loop
    starts, so this exercises the second check site independently."""
    mock_upstreams(schedule_csv(HOME_GAME, SECOND_HOME_GAME))

    deadline = NOW + timedelta(seconds=300)
    calls = {"n": 0}

    def fake_wall_clock() -> datetime:
        calls["n"] += 1
        # Both forecast-loop checks (one per game) pass; the current-
        # conditions loop's first check is past the deadline.
        if calls["n"] <= 2:
            return deadline - timedelta(seconds=1)
        return deadline + timedelta(seconds=1)

    monkeypatch.setattr("weather.capture._wall_clock", fake_wall_clock)

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW, deadline=deadline
        )

    forecast = result["venue_forecast_kickoff"]
    assert forecast.coverage.present == 2

    current = result["venue_conditions_current"]
    assert current.coverage.present < current.coverage.expected
    assert any(e["reason"] == "deadline_exceeded" for e in current.errors)


@respx.mock
async def test_no_deadline_captures_everything():
    """Default behaviour is unchanged when no deadline is supplied."""
    mock_upstreams(postseason_csv(HOME_GAME, SECOND_HOME_GAME))

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, POSTSEASON_WEEK, client=client, lake=NullLakeWriter(), now=NOW
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.present == envelope.coverage.expected == 2
    assert envelope.coverage.missing == []


@respx.mock
async def test_a_mismatched_forecast_hour_is_recorded_per_game_not_raised(
    monkeypatch,
):
    """Guards the `assert_forecast_hour` call inside `capture_week` itself.

    `fetch_forecast_at` always echoes back the requested `valid_at` unchanged,
    so no respx fixture can make the adapter legitimately disagree with
    kickoff — the mismatch has to be injected at the capture layer to prove
    the write-time guard is actually wired in, not just unit-testable in
    isolation.

    FINDING 5: this guard failure used to propagate out of `capture_week`
    entirely and abort the whole week -- one bad game took every other game
    down with it. It must degrade the same way `UnresolvableVenue` does: the
    one game lands in `coverage.missing`/`errors`, and the rest of the week
    still captures.
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
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.present == 0
    assert envelope.coverage.missing == ["2026_01_CHI_CAR"]
    assert envelope.signals == []
    assert any(e["reason"] == "forecast_hour_mismatch" for e in envelope.errors)


class SpyLakeWriter:
    """Records what it was handed, keyed by `lake_key` -- dict-keyed rather
    than a plain list, so a key collision is observable by construction: two
    writes to the same key collapse to one dict entry instead of silently
    both landing. Deliberately NOT moto/S3.

    `collector-core` already tests real object-store semantics against moto,
    and its dev dependencies are not installed for this service in CI — a moto
    import here passes locally off a shared virtualenv and fails in CI, which is
    the worst place to find out. What weather needs to prove is narrower anyway:
    that a capture hands one envelope per signal type to whatever writer it was
    given, under distinct keys. The storage layer's own correctness (S3
    semantics, sorting, prefix scans) is not this service's test to own.
    """

    def __init__(self) -> None:
        self.written: dict[str, object] = {}

    def write(self, envelope) -> str:
        key = lake_key(envelope)
        self.written[key] = envelope
        return key

    def list_keys(self, collector, signal_type, season, week) -> list[str]:
        return [
            key
            for key, envelope in self.written.items()
            if envelope.signal_type == signal_type
        ]

    def read(self, key: str) -> dict:
        return self.written[key].to_dict()


@respx.mock
async def test_capture_writes_one_envelope_per_signal_type_to_the_lake():
    lake = SpyLakeWriter()
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        await capture_week(2026, 1, client=client, lake=lake, now=NOW)

    assert {e.signal_type for e in lake.written.values()} == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }
    assert len(lake.written) == 2


@respx.mock
async def test_capture_pass_produces_two_distinct_lake_keys():
    """FINDING 1 regression: both envelopes from one pass share the same
    `captured_at` by design -- one frozen instant per pass. Before
    `signal_type` was part of the lake key, both writes resolved to the same
    key and the second `put_object` silently overwrote the first, so the
    week's `venue_forecast_kickoff` envelope -- the collector's headline
    product -- vanished from the lake. `SpyLakeWriter`'s dict-keyed storage
    makes that collision observable: a collision here collapses `written` to
    a single entry instead of two.
    """
    lake = SpyLakeWriter()
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        await capture_week(2026, 1, client=client, lake=lake, now=NOW)

    assert len(lake.written) == 2
    assert len(set(lake.written)) == 2  # the keys themselves are distinct


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


# --- the failed-capture path ------------------------------------------------
#
# `capture_week` used to re-raise a schedule-fetch failure WITHOUT writing
# anything, which left the gap in the lake to be inferred from the absence of
# an object -- the one thing the Phase 8 contract says must never be inferred.
# The fix is `collector_core.failure.fail_capture`; these tests pin weather's
# use of it against the real service, while `libs/collector-core/tests/
# test_failure.py` pins the helper itself against a fake collector.


@respx.mock
async def test_a_schedule_outage_still_writes_an_envelope_per_signal_type():
    """A gap must be explicit -- `present: 0` with populated `errors` -- never
    inferred from an absent object."""
    lake = SpyLakeWriter()
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await capture_week(2026, 1, client=client, lake=lake, now=NOW)

    written = list(lake.written.values())
    assert len(written) == 2
    assert {e.signal_type for e in written} == set(SIGNAL_TYPES)
    for envelope in written:
        assert envelope.coverage.present == 0
        assert envelope.signals == []
        assert len(envelope.errors) == 1
        assert envelope.errors[0]["reason"] == "http_status"
        assert envelope.scope == {"season": 2026, "week": 1}


@respx.mock
async def test_a_schedule_outage_reports_a_zero_ratio_not_a_perfect_one():
    """THE property. `Coverage.ratio` reads 1.0 when `expected` is 0, so a
    total outage that reported `expected: 0` would look identical to a healthy
    collector on `collector_coverage_ratio` -- the one metric built to catch
    exactly this."""
    lake = SpyLakeWriter()
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await capture_week(2026, 1, client=client, lake=lake, now=NOW)

    assert lake.written, "a failed capture wrote nothing at all"
    for envelope in lake.written.values():
        assert envelope.coverage.expected == REGULAR_SEASON_GAMES_FLOOR
        assert envelope.coverage.ratio == 0.0


@respx.mock
async def test_a_schedule_outage_re_raises_so_the_cached_capture_survives():
    """`run_capture_loop` catches and leaves `CaptureState` untouched.
    Returning the failure envelopes normally would install them over the last
    good capture -- turning an upstream outage into a loss of *availability*
    on `/signals`, when the whole point of the cache is that an outage costs
    only freshness."""
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(503))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await capture_week(2026, 1, client=client, lake=NullLakeWriter(), now=NOW)


# --- the coverage floor ------------------------------------------------------


@respx.mock
async def test_a_truncated_schedule_does_not_report_perfect_coverage():
    """The ratio-1.0 bug one level up: `expected` derived from the document
    that came back. A schedule feed truncated to one game would otherwise read
    expected=1, present=1, ratio 1.0 -- perfectly healthy, while fifteen games
    silently vanished."""
    mock_upstreams(schedule_csv(HOME_GAME))

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    coverage = result["venue_forecast_kickoff"].coverage
    assert coverage.expected == REGULAR_SEASON_GAMES_FLOOR
    assert coverage.present == 1
    assert coverage.ratio < 0.1


@respx.mock
async def test_the_shortfall_against_the_floor_is_stated_in_errors():
    """`missing` names only the games this pass knows about, so it is short
    while `expected` is not. That gap is real information and is recorded
    rather than left to be inferred from arithmetic."""
    mock_upstreams(schedule_csv(HOME_GAME))

    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    reasons = [e["reason"] for e in result["venue_forecast_kickoff"].errors]
    assert "below_expected_floor" in reasons


def test_the_regular_season_floor_applies_only_to_regular_season_weeks():
    """A postseason week can legitimately hold a single game, and a 13-game
    floor would report a false shortfall every January."""
    assert games_floor(1) == REGULAR_SEASON_GAMES_FLOOR
    assert games_floor(REGULAR_SEASON_WEEKS) == REGULAR_SEASON_GAMES_FLOOR
    assert games_floor(REGULAR_SEASON_WEEKS + 1) == 1
    assert games_floor(0) == 1

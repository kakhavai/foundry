"""Property-based tests for the adapters that parse untrusted upstream payloads.

Ported from the pre-collector `weather/client.py` fuzz suite when Task 13
deleted that module. `client.py`'s parser fuzzed a single Open-Meteo "current
conditions" object and a free-text geocoding response; neither of those
upstreams, nor the functions that called them (`fetch_weather_for_coords`,
`fetch_current_weather`, `GEOCODE_URL`), exist anymore.

What survives is the *shape* of coverage that mattered — "an untrusted payload
with a field missing or wrong-typed must fail loudly, not silently" and "any
well-formed payload maps cleanly to the documented output" — retargeted at the
two adapters that now do this job:

- `weather.adapters.forecast` — Open-Meteo's hourly forecast, keyed by hour.
- `weather.adapters.schedule` — the nflverse schedule CSV.

Dropped outright, not ported: everything that exercised `fetch_current_weather`
and `GEOCODE_URL`. The schedule adapter replaced geocoding entirely — there is
no "look up a free-text location string" code path left for a property test to
target. Fault-injection fuzzing (`FAULT_UPSTREAM_ERROR_RATE` etc.) moved to
`test_faults.py` alongside the rest of that behaviour's coverage, rather than
staying split across two files.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from weather.adapters.forecast import (
    FORECAST_URL,
    fetch_current_conditions,
    fetch_forecast_at,
)
from weather.adapters.schedule import parse_schedule_csv

# Hypothesis drives many examples per test; respx and the event loop are
# function-scoped, so the function_scoped_fixture health check is suppressed.
SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

VALID_AT = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)

REQUIRED_HOURLY_FIELDS = [
    "time",
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "precipitation",
    "precipitation_probability",
]


def _valid_hourly() -> dict:
    return {
        "hourly": {
            "time": ["2026-09-13T17:00"],
            "temperature_2m": [68.0],
            "apparent_temperature": [67.0],
            "relative_humidity_2m": [62],
            "wind_speed_10m": [11.0],
            "wind_gusts_10m": [18.0],
            "wind_direction_10m": [210],
            "precipitation": [0.0],
            "precipitation_probability": [10],
        }
    }


@SETTINGS
@given(missing=st.sampled_from(REQUIRED_HOURLY_FIELDS))
@respx.mock
async def test_missing_hourly_field_raises_keyerror(missing):
    """A field dropped from the upstream's hourly block must fail loudly, not
    silently publish a partial or wrong forecast reading."""
    payload = _valid_hourly()
    del payload["hourly"][missing]
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=payload))

    async with httpx.AsyncClient() as client:
        with pytest.raises(KeyError):
            await fetch_forecast_at(35.2, -80.8, VALID_AT, client)


@SETTINGS
@given(status=st.integers(min_value=400, max_value=599))
@respx.mock
async def test_upstream_error_status_raises(status):
    """Every 4xx/5xx from Open-Meteo surfaces as HTTPStatusError."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(status))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_forecast_at(35.2, -80.8, VALID_AT, client)


@SETTINGS
@given(
    body=st.one_of(
        st.lists(st.integers(), max_size=3),
        st.text(max_size=20),
        st.integers(),
    )
)
@respx.mock
async def test_non_object_body_raises_typeerror(body):
    """A JSON body that is not an object must not produce a silent success."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=body))

    async with httpx.AsyncClient() as client:
        with pytest.raises((TypeError, KeyError)):
            await fetch_forecast_at(35.2, -80.8, VALID_AT, client)


@respx.mock
async def test_json_null_body_raises_typeerror():
    """A literal JSON `null` from the upstream must fail loudly, not silently.

    Note `content=b"null"`, not `json=None` — httpx treats `json=None` as
    "no payload supplied" and sends an empty body instead.
    """
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, content=b"null"))

    async with httpx.AsyncClient() as client:
        with pytest.raises(TypeError):
            await fetch_forecast_at(35.2, -80.8, VALID_AT, client)


@SETTINGS
@given(
    temp=st.floats(min_value=-40, max_value=130, allow_nan=False, allow_infinity=False),
    feels_like=st.floats(
        min_value=-40, max_value=130, allow_nan=False, allow_infinity=False
    ),
    humidity=st.floats(
        min_value=0, max_value=100, allow_nan=False, allow_infinity=False
    ),
    wind=st.floats(min_value=0, max_value=200, allow_nan=False, allow_infinity=False),
    gust=st.floats(min_value=0, max_value=250, allow_nan=False, allow_infinity=False),
    direction=st.integers(min_value=0, max_value=359),
    precip_rate=st.floats(
        min_value=0, max_value=5, allow_nan=False, allow_infinity=False
    ),
    precip_prob=st.integers(min_value=0, max_value=100),
)
@respx.mock
async def test_wellformed_payload_always_maps_to_documented_fields(
    temp, feels_like, humidity, wind, gust, direction, precip_rate, precip_prob
):
    """Any structurally valid hourly payload maps to the documented output
    keys, in the same imperial units the upstream already reports them in —
    the adapter's job is selecting the right hour, not converting units."""
    payload = {
        "hourly": {
            "time": ["2026-09-13T17:00"],
            "temperature_2m": [temp],
            "apparent_temperature": [feels_like],
            "relative_humidity_2m": [humidity],
            "wind_speed_10m": [wind],
            "wind_gusts_10m": [gust],
            "wind_direction_10m": [direction],
            "precipitation": [precip_rate],
            "precipitation_probability": [precip_prob],
        }
    }
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=payload))

    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

    assert result["temperature_f"] == temp
    assert result["feels_like_f"] == feels_like
    assert result["wind_speed_mph"] == wind
    assert result["wind_gust_mph"] == gust
    assert result["wind_direction_deg"] == direction
    assert result["precipitation_rate_in_hr"] == precip_rate
    assert result["precipitation_probability"] == precip_prob / 100.0
    assert result["humidity_pct"] == humidity
    assert result["precipitation_type"] in {
        "none",
        "rain",
        "snow",
        "sleet",
        "freezing_rain",
    }


@SETTINGS
@given(
    minute=st.integers(min_value=0, max_value=59),
    second=st.integers(min_value=0, max_value=59),
    microsecond=st.integers(min_value=0, max_value=999_999),
)
@respx.mock
async def test_current_conditions_always_requests_the_truncated_hour(
    minute, second, microsecond
):
    """New in Task 13 (Step 3b): `fetch_current_conditions` takes its reference
    time as a parameter instead of reading the wall clock. Whatever minute or
    second `now` lands on, the request must land on the top of that hour — not
    a neighbouring one — or a caller a few seconds either side of the hour
    boundary gets the wrong reading. The payload here has exactly one hour on
    offer, so the call only succeeds if the truncation is exact.
    """
    now = datetime(2026, 9, 13, 17, minute, second, microsecond, tzinfo=UTC)
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=_valid_hourly()))

    async with httpx.AsyncClient() as client:
        result = await fetch_current_conditions(35.2, -80.8, client, now=now)

    assert result["temperature_f"] == 68.0
    assert "bands" not in result


# --- weather.adapters.schedule -----------------------------------------

SCHEDULE_HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)

# CSV-safe free text: no field separator, no quoting, no bare newlines, no
# surrogate halves that would fail to encode.
_SAFE_TEXT = (
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",), blacklist_characters=',"\r\n'
        ),
        min_size=1,
        max_size=12,
    )
    .map(lambda s: s.strip())
    .filter(lambda s: s != "")
)


def _schedule_row(
    game_id: str,
    home_team: str,
    away_team: str,
    stadium_id: str,
    stadium_name: str,
    roof: str,
    location: str,
) -> str:
    return (
        f"{game_id},2026,REG,1,2026-09-13,13:00,{away_team},{home_team},"
        f"{location},{roof},grass,{stadium_id},{stadium_name}"
    )


@SETTINGS
@given(
    game_id=_SAFE_TEXT,
    home_team=_SAFE_TEXT,
    away_team=_SAFE_TEXT,
    stadium_id=_SAFE_TEXT,
    stadium_name=_SAFE_TEXT,
    roof=st.sampled_from(["outdoors", "dome", "closed", "open"]),
)
def test_non_neutral_rows_keep_stadium_id_and_roof(
    game_id, home_team, away_team, stadium_id, stadium_name, roof
):
    text = (
        SCHEDULE_HEADER
        + "\n"
        + _schedule_row(
            game_id, home_team, away_team, stadium_id, stadium_name, roof, "Home"
        )
        + "\n"
    )
    (game,) = parse_schedule_csv(text, season=2026, week=1)
    assert game.game_id == game_id
    assert game.stadium_id == stadium_id
    assert game.roof_raw == roof
    assert game.is_neutral_site is False


@SETTINGS
@given(
    game_id=_SAFE_TEXT,
    home_team=_SAFE_TEXT,
    away_team=_SAFE_TEXT,
    stadium_id=_SAFE_TEXT,
    stadium_name=_SAFE_TEXT,
    roof=st.sampled_from(["outdoors", "dome", "closed", "open", ""]),
    location=st.sampled_from(["Neutral", "neutral", " NEUTRAL ", "NeUtRaL"]),
)
def test_neutral_rows_always_discard_stadium_id_and_roof(
    game_id, home_team, away_team, stadium_id, stadium_name, roof, location
):
    """However the feed spells "neutral" and whatever venue/roof it reports for
    the designated home team, a neutral-site row must never let them through —
    trusting them fetches the wrong city's weather by thousands of miles."""
    text = (
        SCHEDULE_HEADER
        + "\n"
        + _schedule_row(
            game_id, home_team, away_team, stadium_id, stadium_name, roof, location
        )
        + "\n"
    )
    (game,) = parse_schedule_csv(text, season=2026, week=1)
    assert game.is_neutral_site is True
    assert game.stadium_id is None
    assert game.roof_raw is None
    assert game.stadium_name == stadium_name

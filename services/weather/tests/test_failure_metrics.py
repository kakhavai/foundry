"""Capture-failure metrics: every failure class the classifier recognizes must
land under its own `reason` label, and a successful call must count as an
attempt without being miscounted as a failure.

Previously exercised through `/weather/stadiums`, which called the upstream
per request and swallowed a failing stadium into an always-200 response with
`weather: None` — the counters were the only place total upstream failure was
visible. Task 13 deleted that route: `/signals` now serves from a cache and
never calls upstream itself, and the upstream-calling code moved to
`weather.capture.capture_week`, which already records these same counters
(`metrics.record_upstream_attempt` / `record_upstream_failure`) around its
forecast and current-conditions fetches. This file now drives `capture_week`
directly instead of going through HTTP, since that's the layer that actually
calls the metrics functions.

Dropped: the old "every stadium failure is counted even though the response is
200" test. Its premise — a response that always looks healthy no matter how
badly the upstream failed — doesn't exist anymore. `capture_week` records a
failure into the envelope's own `errors` list (already covered by
`test_capture.py::test_total_upstream_failure_still_writes_an_envelope`), so a
total outage is visible in the response body itself, not just in a side-channel
counter.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from collector_core.lake import NullLakeWriter

from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL
from weather.capture import capture_week
from weather.metrics import metrics as weather_metrics

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

SCHEDULE_HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)
HOME_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)


def schedule_csv(*rows: str) -> str:
    return "\n".join([SCHEDULE_HEADER, *rows]) + "\n"


def hourly_payload() -> dict:
    times = [f"2026-09-13T{h:02d}:00" for h in range(24)] + [
        f"2026-09-11T{h:02d}:00" for h in range(24)
    ]
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


@pytest.mark.parametrize(
    "mock_kwargs, reason",
    [
        ({"return_value": httpx.Response(500)}, "http_status"),
        ({"side_effect": httpx.ConnectTimeout("timed out")}, "timeout"),
        ({"side_effect": httpx.ConnectError("refused")}, "transport"),
        ({"return_value": httpx.Response(200, json={"nope": {}})}, "malformed"),
    ],
)
@respx.mock
async def test_each_failure_class_increments_its_own_reason(
    metric_value, mock_kwargs, reason
):
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=schedule_csv(HOME_GAME))
    )
    respx.get(FORECAST_URL).mock(**mock_kwargs)

    before = (
        metric_value(
            "collector_capture_failures_total", collector="weather", reason=reason
        )
        or 0.0
    )
    async with httpx.AsyncClient() as client:
        await capture_week(2026, 1, client=client, lake=NullLakeWriter(), now=NOW)
    after = (
        metric_value(
            "collector_capture_failures_total", collector="weather", reason=reason
        )
        or 0.0
    )

    # One game -> one forecast attempt. Venue resolution now happens before
    # the forecast is attempted (FINDING 1: a venue must count toward
    # current-conditions coverage even when its forecast fails), so the venue
    # is still resolved and current conditions is attempted against the same
    # broken endpoint -- two failures of this reason, not one.
    assert after - before == 2.0


@respx.mock
async def test_successful_calls_count_as_attempts_but_not_failures(metric_value):
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=schedule_csv(HOME_GAME))
    )
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=hourly_payload())
    )

    attempts_before = (
        metric_value("collector_capture_requests_total", collector="weather") or 0.0
    )
    failures_before = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="transport",
        )
        or 0.0
    )
    async with httpx.AsyncClient() as client:
        await capture_week(2026, 1, client=client, lake=NullLakeWriter(), now=NOW)
    attempts_after = (
        metric_value("collector_capture_requests_total", collector="weather") or 0.0
    )
    failures_after = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="transport",
        )
        or 0.0
    )

    # One game: one forecast attempt plus one current-conditions attempt for
    # its venue.
    assert attempts_after - attempts_before == 2.0
    assert failures_after - failures_before == 0.0


@respx.mock
async def test_schedule_feed_failure_increments_the_failure_counter(metric_value):
    """FINDING 5: `fetch_schedule` used to sit outside every try/except in
    `capture_week` -- a total schedule-feed outage propagated to the caller
    (logged and swallowed) without incrementing a single counter. The
    observable state was pod Healthy, `/signals` empty, and nothing in
    `/metrics` to alert on. This is the most likely total-outage mode short
    of the lake itself being unreachable.
    """
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(503))

    before = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="http_status",
        )
        or 0.0
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await capture_week(2026, 1, client=client, lake=NullLakeWriter(), now=NOW)
    after = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="http_status",
        )
        or 0.0
    )

    assert after - before == 1.0


def test_unknown_exception_class_falls_back_to_unknown():
    """The fallback is unreachable through capture_week — every exception type
    the classifier is asked to handle there is already covered above — so it
    is asserted directly. It exists so the classifier can never itself raise
    inside a failure handler.
    """
    assert weather_metrics._reason(RuntimeError("something unforeseen")) == "unknown"

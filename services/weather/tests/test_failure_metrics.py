import httpx
import pytest
import respx

from weather import metrics as weather_metrics
from weather.stadiums import STADIUMS

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

_GOOD_BODY = {
    "current": {
        "time": "2026-07-28T12:00",
        "temperature_2m": 18.5,
        "relative_humidity_2m": 65,
        "wind_speed_10m": 12.3,
        "weather_code": 2,
        "precipitation": 0.0,
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
def test_each_failure_class_increments_its_own_reason(
    metric_value, client, mock_kwargs, reason
):
    respx.get(WEATHER_URL).mock(**mock_kwargs)

    before = (
        metric_value(
            "collector_capture_failures_total", collector="weather", reason=reason
        )
        or 0.0
    )
    response = client.get("/weather/stadiums/lambeau")
    after = (
        metric_value(
            "collector_capture_failures_total", collector="weather", reason=reason
        )
        or 0.0
    )

    assert response.status_code == 502
    assert after - before == 1.0


@respx.mock
def test_every_stadium_failure_is_counted_even_though_the_response_is_200(
    metric_value, client
):
    """The blind spot this metric exists to close.

    /weather/stadiums swallows per-stadium failures and substitutes None, so it
    returns 200 with count == 30 whether thirty stadiums resolved or zero did —
    and smoke-test.sh asserts exactly that count. Without this counter, total
    upstream failure is indistinguishable from full success.
    """
    respx.get(WEATHER_URL).mock(side_effect=httpx.ConnectError("refused"))

    before = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="transport",
        )
        or 0.0
    )
    response = client.get("/weather/stadiums")
    after = (
        metric_value(
            "collector_capture_failures_total",
            collector="weather",
            reason="transport",
        )
        or 0.0
    )

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == len(STADIUMS)
    assert all(s["weather"] is None for s in body["stadiums"])
    assert after - before == float(len(STADIUMS))


@respx.mock
def test_successful_calls_count_as_attempts_but_not_failures(metric_value, client):
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=_GOOD_BODY))

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
    response = client.get("/weather/stadiums/lambeau")
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

    assert response.status_code == 200
    assert attempts_after - attempts_before == 1.0
    assert failures_after - failures_before == 0.0


def test_unknown_exception_class_falls_back_to_unknown():
    """The fallback is unreachable through the routes — every exception type the
    handlers catch classifies above it — so it is asserted directly. It exists so
    the classifier can never itself raise inside a failure handler.
    """
    assert weather_metrics._reason(RuntimeError("something unforeseen")) == "unknown"

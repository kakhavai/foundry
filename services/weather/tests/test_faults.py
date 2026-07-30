"""Env-var-guarded fault injection for the Incident Detection eval harness.

Retargeted from `weather.client._maybe_inject_fault` (deleted in Task 13) to
`weather.adapters.forecast._maybe_inject_fault` — the same behaviour, moved
along with the code that used to host it. The Hypothesis-driven fault tests
that used to live in `test_properties.py` moved here too, so every test that
exercises fault injection lives in one file instead of being split by
accident of which module they originally imported from.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from weather.adapters.forecast import FORECAST_URL, fetch_forecast_at

SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

VALID_AT = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)

GOOD_HOURLY = {
    "hourly": {
        "time": ["2026-09-13T17:00"],
        "temperature_2m": [18.5],
        "apparent_temperature": [17.0],
        "relative_humidity_2m": [65],
        "wind_speed_10m": [12.3],
        "wind_gusts_10m": [18.0],
        "wind_direction_10m": [210],
        "precipitation": [0.0],
        "precipitation_probability": [10],
    }
}


@respx.mock
async def test_no_fault_env_passes_through(monkeypatch):
    monkeypatch.delenv("FAULT_UPSTREAM_ERROR_RATE", raising=False)
    monkeypatch.delenv("FAULT_UPSTREAM_LATENCY_MS", raising=False)
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=GOOD_HOURLY))

    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(40.0, -75.0, VALID_AT, client)

    assert result["temperature_f"] == 18.5


async def test_fault_error_rate_one_raises(monkeypatch):
    monkeypatch.setenv("FAULT_UPSTREAM_ERROR_RATE", "1.0")

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_forecast_at(40.0, -75.0, VALID_AT, client)


@SETTINGS
@given(rate=st.floats(min_value=0.01, max_value=1.0))
@respx.mock
async def test_fault_error_rate_injects_503(monkeypatch, rate):
    """Any non-zero FAULT_UPSTREAM_ERROR_RATE raises when the draw falls under it.

    `random.random()` is pinned to 0.0 so the branch is deterministic — asserting
    on real randomness would make this test flaky at exactly the rate it claims
    to verify.
    """
    monkeypatch.setattr("weather.adapters.forecast.random.random", lambda: 0.0)
    monkeypatch.setenv("FAULT_UPSTREAM_ERROR_RATE", str(rate))
    route = respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json={}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await fetch_forecast_at(40.0, -75.0, VALID_AT, client)

    assert exc.value.response.status_code == 503
    assert not route.called  # fault short-circuits before the upstream request


@respx.mock
async def test_fault_error_rate_not_drawn_lets_request_through(monkeypatch):
    """The other side of the branch: draw above the rate means no fault."""
    monkeypatch.setattr("weather.adapters.forecast.random.random", lambda: 0.99)
    monkeypatch.setenv("FAULT_UPSTREAM_ERROR_RATE", "0.5")
    route = respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=GOOD_HOURLY)
    )

    async with httpx.AsyncClient() as client:
        await fetch_forecast_at(40.0, -75.0, VALID_AT, client)

    assert route.called


async def test_fault_latency_delays_call(monkeypatch):
    """FAULT_UPSTREAM_LATENCY_MS sleeps for the configured duration."""
    slept = []
    monkeypatch.setenv("FAULT_UPSTREAM_LATENCY_MS", "250")

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("weather.adapters.forecast.asyncio.sleep", fake_sleep)

    with respx.mock:
        respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=GOOD_HOURLY))
        async with httpx.AsyncClient() as client:
            await fetch_forecast_at(40.0, -75.0, VALID_AT, client)

    assert slept == [0.25]


async def test_no_fault_env_vars_is_inert(monkeypatch):
    """With no FAULT_* vars set, the injector must not sleep or raise."""
    monkeypatch.delenv("FAULT_UPSTREAM_LATENCY_MS", raising=False)
    monkeypatch.delenv("FAULT_UPSTREAM_ERROR_RATE", raising=False)

    with respx.mock:
        route = respx.get(FORECAST_URL).mock(
            return_value=httpx.Response(200, json=GOOD_HOURLY)
        )
        async with httpx.AsyncClient() as client:
            await fetch_forecast_at(40.0, -75.0, VALID_AT, client)

    assert route.called

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from weather.client import (
    GEOCODE_URL,
    WEATHER_URL,
    fetch_current_weather,
    fetch_weather_for_coords,
)

# Hypothesis drives many examples per test; respx and the event loop are
# function-scoped, so the function_scoped_fixture health check is suppressed.
SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

REQUIRED_CURRENT_FIELDS = [
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "weather_code",
    "precipitation",
    "time",
]


@SETTINGS
@given(missing=st.sampled_from(REQUIRED_CURRENT_FIELDS))
@respx.mock
async def test_missing_current_field_raises_keyerror(missing):
    """A field dropped by the upstream must fail loudly, not return a partial dict."""
    payload = {
        "current": {
            "temperature_2m": 12.0,
            "relative_humidity_2m": 55,
            "wind_speed_10m": 9.0,
            "weather_code": 3,
            "precipitation": 0.0,
            "time": "2026-09-30T14:00",
        }
    }
    del payload["current"][missing]
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=payload))

    async with httpx.AsyncClient() as client:
        with pytest.raises(KeyError):
            await fetch_weather_for_coords(37.7, -122.4, client)


@SETTINGS
@given(status=st.integers(min_value=400, max_value=599))
@respx.mock
async def test_upstream_error_status_raises(status):
    """Every 4xx/5xx from Open-Meteo surfaces as HTTPStatusError."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(status))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_weather_for_coords(37.7, -122.4, client)


@SETTINGS
@given(
    body=st.one_of(
        st.lists(st.integers(), max_size=3),
        st.text(max_size=20),
        st.integers(),
    )
)
@respx.mock
async def test_non_object_body_raises_typeerror_or_keyerror(body):
    """A JSON body that is not an object must not produce a silent success."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=body))

    async with httpx.AsyncClient() as client:
        with pytest.raises((TypeError, KeyError)):
            await fetch_weather_for_coords(37.7, -122.4, client)


@SETTINGS
@given(
    temp=st.floats(min_value=-100, max_value=100, allow_nan=False),
    humidity=st.integers(min_value=0, max_value=100),
    wind=st.floats(min_value=0, max_value=500, allow_nan=False),
    code=st.integers(min_value=0, max_value=99),
    precip=st.floats(min_value=0, max_value=1000, allow_nan=False),
)
@respx.mock
async def test_wellformed_payload_always_maps_cleanly(
    temp, humidity, wind, code, precip
):
    """Any structurally valid payload maps to the documented output keys."""
    respx.get(WEATHER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": temp,
                    "relative_humidity_2m": humidity,
                    "wind_speed_10m": wind,
                    "weather_code": code,
                    "precipitation": precip,
                    "time": "2026-09-30T14:00",
                }
            },
        )
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_weather_for_coords(37.7, -122.4, client)

    assert result == {
        "temperature_c": temp,
        "relative_humidity_pct": humidity,
        "wind_speed_kmh": wind,
        "weather_code": code,
        "precipitation_mm": precip,
        "time": "2026-09-30T14:00",
    }


@SETTINGS
@given(location=st.text(min_size=1, max_size=30))
@respx.mock
async def test_empty_geocode_results_raise_valueerror(location):
    """An unknown location is a ValueError, never an IndexError."""
    respx.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Location not found"):
            await fetch_current_weather(location, client)


@respx.mock
async def test_geocode_missing_results_key_raises_valueerror():
    """A payload with no `results` key at all behaves the same as an empty list."""
    respx.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Location not found"):
            await fetch_current_weather("nowhere", client)


@respx.mock
async def test_fetch_current_weather_success_path():
    """A valid geocode result triggers the weather fetch and returns enriched data."""
    respx.get(GEOCODE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "San Francisco",
                        "latitude": 37.7749,
                        "longitude": -122.4194,
                    }
                ]
            },
        )
    )
    respx.get(WEATHER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 16.0,
                    "relative_humidity_2m": 72,
                    "wind_speed_10m": 8.5,
                    "weather_code": 1,
                    "precipitation": 0.0,
                    "time": "2026-09-30T14:00",
                }
            },
        )
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_current_weather("San Francisco", client)

    assert result == {
        "location": "San Francisco",
        "latitude": 37.7749,
        "longitude": -122.4194,
        "temperature_c": 16.0,
        "relative_humidity_pct": 72,
        "wind_speed_kmh": 8.5,
        "weather_code": 1,
        "precipitation_mm": 0.0,
        "time": "2026-09-30T14:00",
    }


@SETTINGS
@given(rate=st.floats(min_value=0.01, max_value=1.0))
@respx.mock
async def test_fault_error_rate_injects_503(monkeypatch, rate):
    """Any non-zero FAULT_UPSTREAM_ERROR_RATE raises when the draw falls under it.

    `random.random()` is pinned to 0.0 so the branch is deterministic — asserting
    on real randomness would make this test flaky at exactly the rate it claims
    to verify.
    """
    monkeypatch.setattr("weather.client.random.random", lambda: 0.0)
    monkeypatch.setenv("FAULT_UPSTREAM_ERROR_RATE", str(rate))
    route = respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json={}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await fetch_weather_for_coords(37.7, -122.4, client)

    assert exc.value.response.status_code == 503
    assert not route.called  # fault short-circuits before the upstream request


@respx.mock
async def test_fault_error_rate_not_drawn_lets_request_through(monkeypatch):
    """The other side of the branch: draw above the rate means no fault."""
    monkeypatch.setattr("weather.client.random.random", lambda: 0.99)
    monkeypatch.setenv("FAULT_UPSTREAM_ERROR_RATE", "0.5")
    route = respx.get(WEATHER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 1.0,
                    "relative_humidity_2m": 1,
                    "wind_speed_10m": 1.0,
                    "weather_code": 1,
                    "precipitation": 0.0,
                    "time": "t",
                }
            },
        )
    )

    async with httpx.AsyncClient() as client:
        await fetch_weather_for_coords(37.7, -122.4, client)

    assert route.called


async def test_fault_latency_delays_call(monkeypatch):
    """FAULT_UPSTREAM_LATENCY_MS sleeps for the configured duration."""
    slept = []
    monkeypatch.setenv("FAULT_UPSTREAM_LATENCY_MS", "250")

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("weather.client.asyncio.sleep", fake_sleep)

    with respx.mock:
        respx.get(WEATHER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "current": {
                        "temperature_2m": 1.0,
                        "relative_humidity_2m": 1,
                        "wind_speed_10m": 1.0,
                        "weather_code": 1,
                        "precipitation": 0.0,
                        "time": "t",
                    }
                },
            )
        )
        async with httpx.AsyncClient() as client:
            await fetch_weather_for_coords(37.7, -122.4, client)

    assert slept == [0.25]


async def test_no_fault_env_vars_is_inert(monkeypatch):
    """With no FAULT_* vars set, the injector must not sleep or raise."""
    monkeypatch.delenv("FAULT_UPSTREAM_LATENCY_MS", raising=False)
    monkeypatch.delenv("FAULT_UPSTREAM_ERROR_RATE", raising=False)

    with respx.mock:
        route = respx.get(WEATHER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "current": {
                        "temperature_2m": 1.0,
                        "relative_humidity_2m": 1,
                        "wind_speed_10m": 1.0,
                        "weather_code": 1,
                        "precipitation": 0.0,
                        "time": "t",
                    }
                },
            )
        )
        async with httpx.AsyncClient() as client:
            await fetch_weather_for_coords(37.7, -122.4, client)

    assert route.called

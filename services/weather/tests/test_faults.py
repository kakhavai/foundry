import httpx
import pytest
import respx

from weather.client import fetch_weather_for_coords


@respx.mock
async def test_no_fault_env_passes_through(monkeypatch):
    monkeypatch.delenv("FAULT_UPSTREAM_ERROR_RATE", raising=False)
    monkeypatch.delenv("FAULT_UPSTREAM_LATENCY_MS", raising=False)
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(
            200,
            json={
                "current": {
                    "time": "2026-06-14T12:00",
                    "temperature_2m": 18.5,
                    "relative_humidity_2m": 65,
                    "wind_speed_10m": 12.3,
                    "weather_code": 2,
                    "precipitation": 0.0,
                }
            },
        )
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_weather_for_coords(40.0, -75.0, client)
    assert result["temperature_c"] == 18.5


async def test_fault_error_rate_one_raises(monkeypatch):
    monkeypatch.setenv("FAULT_UPSTREAM_ERROR_RATE", "1.0")
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_weather_for_coords(40.0, -75.0, client)

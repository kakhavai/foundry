from datetime import UTC, datetime

import httpx
import pytest
import respx

from weather.adapters.forecast import (
    FORECAST_URL,
    ForecastHorizonError,
    fetch_forecast_at,
)

VALID_AT = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)

HOURLY = {
    "hourly": {
        "time": ["2026-09-13T16:00", "2026-09-13T17:00", "2026-09-13T18:00"],
        "temperature_2m": [66.0, 68.0, 70.0],
        "apparent_temperature": [64.0, 67.0, 69.0],
        "relative_humidity_2m": [60, 62, 63],
        "wind_speed_10m": [8.0, 11.0, 12.0],
        "wind_gusts_10m": [14.0, 18.0, 19.0],
        "wind_direction_10m": [200, 210, 215],
        "precipitation": [0.0, 0.02, 0.05],
        "precipitation_probability": [5, 20, 35],
    }
}


@respx.mock
async def test_selects_the_requested_hour_not_the_first_one():
    """Asking for a daily summary and taking element zero is the bug this
    guards: it silently returns the wrong hour and looks entirely normal."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

    assert result["temperature_f"] == 68.0
    assert result["wind_speed_mph"] == 11.0
    assert result["wind_direction_deg"] == 210


@respx.mock
async def test_forecast_valid_at_echoes_the_requested_hour():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

    assert result["forecast_valid_at"] == VALID_AT


@respx.mock
async def test_requests_imperial_units():
    """The envelope standardizes on suffixed imperial fields. The service used
    to emit Celsius and km/h; unit drift here is silent and catastrophic."""
    route = respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

    params = route.calls.last.request.url.params
    assert params["temperature_unit"] == "fahrenheit"
    assert params["wind_speed_unit"] == "mph"
    assert params["precipitation_unit"] == "inch"


@respx.mock
async def test_missing_requested_hour_raises_rather_than_falling_back():
    """An adapter past its model horizon must fail loudly. Returning the
    nearest available hour is how a nowcast gets published as a forecast."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ForecastHorizonError):
            await fetch_forecast_at(
                35.2, -80.8, datetime(2026, 9, 20, 17, 0, tzinfo=UTC), client
            )


@respx.mock
async def test_bands_widen_with_lead_time():
    """A genuine forecast series narrows as kickoff approaches. Flat bands mean
    the collector is republishing current conditions."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client, lead_hours=96)
        near = await fetch_forecast_at(35.2, -80.8, VALID_AT, client, lead_hours=2)

    far_width = (
        result["bands"]["wind_speed_mph"]["p90"]
        - result["bands"]["wind_speed_mph"]["p10"]
    )
    near_width = (
        near["bands"]["wind_speed_mph"]["p90"] - near["bands"]["wind_speed_mph"]["p10"]
    )
    assert far_width > near_width


@respx.mock
async def test_precipitation_type_is_derived_from_temperature():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client)
    assert result["precipitation_type"] in {
        "none",
        "rain",
        "snow",
        "sleet",
        "freezing_rain",
        "mixed",
    }


@respx.mock
async def test_upstream_error_propagates():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

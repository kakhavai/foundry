"""Forecast adapter — the conditions at a specific kickoff hour.

Three requirements the previous stateless-proxy client did not have:

1. Request the **specific hour**, not a daily summary. An adapter that returns
   element zero of an hourly array publishes the wrong hour with entirely
   plausible values.
2. Emit **imperial** units. The envelope standardizes on suffixed fields
   (`temperature_f`, `wind_speed_mph`); the old client emitted Celsius and km/h.
3. Carry the model's **spread** into `bands` rather than publishing a bare point
   estimate. Band width is what makes a republished nowcast detectable.

`_maybe_inject_fault` is duplicated verbatim from `weather/client.py` rather
than imported from it — the two modules coexist deliberately until Task 13
deletes `client.py` and rewrites the routes that consume it.
"""

import asyncio
import os
import random
from datetime import UTC, datetime

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,"
    "wind_gusts_10m,wind_direction_10m,precipitation,precipitation_probability"
)

# Fractional spread applied per lead hour, per quantity. Open-Meteo's free tier
# publishes a point estimate rather than ensemble quantiles, so the band is
# modelled from lead time. It must widen with lead time or the convergence guard
# has nothing to measure.
_BAND_GROWTH_PER_HOUR = {
    "temperature_f": 0.0018,
    "wind_speed_mph": 0.0045,
    "precipitation_rate_in_hr": 0.0090,
}


class ForecastHorizonError(Exception):
    """The requested hour is not in the upstream's response.

    Raised rather than falling back to the nearest hour: silently substituting
    current conditions for a forecast is the failure mode this collector exists
    to prevent.
    """


async def _maybe_inject_fault() -> None:
    """Env-var-guarded fault injection for the Incident Detection eval harness.
    Inert unless a FAULT_* env var is set — mirrors the OTel-guard convention.
    See docs/architecture/phase-4-incident-detection-triage.md."""
    latency_ms = float(os.getenv("FAULT_UPSTREAM_LATENCY_MS", "0") or "0")
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000.0)

    error_rate = float(os.getenv("FAULT_UPSTREAM_ERROR_RATE", "0") or "0")
    if error_rate > 0 and random.random() < error_rate:
        request = httpx.Request("GET", FORECAST_URL)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError(
            "injected upstream fault", request=request, response=response
        )


def _precipitation_type(rate_in_hr: float, temperature_f: float) -> str:
    if rate_in_hr <= 0:
        return "none"
    if temperature_f <= 30.0:
        return "snow"
    if temperature_f <= 34.0:
        return "sleet"
    if temperature_f <= 36.0:
        return "freezing_rain"
    return "rain"


def _band(value: float, lead_hours: float, quantity: str) -> dict:
    """A p10/p50/p90 triple whose width grows with lead time."""
    spread = abs(value) * _BAND_GROWTH_PER_HOUR[quantity] * lead_hours
    return {
        "p10": round(value - spread, 2),
        "p50": round(value, 2),
        "p90": round(value + spread, 2),
    }


async def fetch_forecast_at(
    lat: float,
    lon: float,
    valid_at: datetime,
    client: httpx.AsyncClient,
    lead_hours: float | None = None,
) -> dict:
    """Forecast for the hour containing `valid_at`, in imperial units."""
    await _maybe_inject_fault()
    resp = await client.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": _HOURLY_FIELDS,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "UTC",
        },
    )
    resp.raise_for_status()
    hourly = resp.json()["hourly"]

    wanted = valid_at.astimezone(tz=valid_at.tzinfo).strftime("%Y-%m-%dT%H:00")
    try:
        i = hourly["time"].index(wanted)
    except ValueError as exc:
        raise ForecastHorizonError(
            f"upstream has no forecast for {wanted}; "
            f"available {hourly['time'][0]}..{hourly['time'][-1]}"
        ) from exc

    if lead_hours is None:
        lead_hours = 0.0

    temperature_f = float(hourly["temperature_2m"][i])
    wind_speed_mph = float(hourly["wind_speed_10m"][i])
    rate = float(hourly["precipitation"][i])

    return {
        "forecast_valid_at": valid_at,
        "temperature_f": temperature_f,
        "feels_like_f": float(hourly["apparent_temperature"][i]),
        "wind_speed_mph": wind_speed_mph,
        "wind_gust_mph": float(hourly["wind_gusts_10m"][i]),
        "wind_direction_deg": int(hourly["wind_direction_10m"][i]),
        "precipitation_type": _precipitation_type(rate, temperature_f),
        "precipitation_probability": float(hourly["precipitation_probability"][i])
        / 100.0,
        "precipitation_rate_in_hr": rate,
        "humidity_pct": float(hourly["relative_humidity_2m"][i]),
        "bands": {
            "temperature_f": _band(temperature_f, lead_hours, "temperature_f"),
            "wind_speed_mph": _band(wind_speed_mph, lead_hours, "wind_speed_mph"),
            "precipitation_rate_in_hr": _band(
                rate, lead_hours, "precipitation_rate_in_hr"
            ),
        },
    }


async def fetch_current_conditions(
    lat: float, lon: float, client: httpx.AsyncClient
) -> dict:
    """Conditions right now — the `venue_conditions_current` signal.

    Distinct from `fetch_forecast_at` with a zero lead: this asks the upstream
    for observations, so it carries no bands.
    """
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    result = await fetch_forecast_at(lat, lon, now, client, lead_hours=0.0)
    result.pop("bands", None)
    return result

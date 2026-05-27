import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

_CURRENT_FIELDS = (
    "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code,precipitation"
)


async def fetch_weather_for_coords(
    lat: float, lon: float, client: httpx.AsyncClient
) -> dict:
    """Fetch current weather conditions for a known lat/lon. Core weather primitive."""
    resp = await client.get(
        WEATHER_URL,
        params={"latitude": lat, "longitude": lon, "current": _CURRENT_FIELDS},
    )
    resp.raise_for_status()
    current = resp.json()["current"]
    return {
        "temperature_c": current["temperature_2m"],
        "relative_humidity_pct": current["relative_humidity_2m"],
        "wind_speed_kmh": current["wind_speed_10m"],
        "weather_code": current["weather_code"],
        "precipitation_mm": current["precipitation"],
        "time": current["time"],
    }


async def fetch_current_weather(location: str, client: httpx.AsyncClient) -> dict:
    """Geocode a location string then fetch current weather. Internal utility only."""
    geo_resp = await client.get(GEOCODE_URL, params={"name": location, "count": 1})
    geo_resp.raise_for_status()
    results = geo_resp.json().get("results", [])
    if not results:
        raise ValueError(f"Location not found: {location}")
    place = results[0]
    weather = await fetch_weather_for_coords(
        place["latitude"], place["longitude"], client
    )
    return {
        "location": place["name"],
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        **weather,
    }

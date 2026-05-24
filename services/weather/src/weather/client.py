import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

_CURRENT_FIELDS = (
    "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code,precipitation"
)


async def fetch_current_weather(location: str, client: httpx.AsyncClient) -> dict:
    geo_resp = await client.get(GEOCODE_URL, params={"name": location, "count": 1})
    geo_resp.raise_for_status()
    results = geo_resp.json().get("results", [])
    if not results:
        raise ValueError(f"Location not found: {location}")

    place = results[0]
    lat, lon = place["latitude"], place["longitude"]

    wx_resp = await client.get(
        WEATHER_URL,
        params={"latitude": lat, "longitude": lon, "current": _CURRENT_FIELDS},
    )
    wx_resp.raise_for_status()
    current = wx_resp.json()["current"]

    return {
        "location": place["name"],
        "latitude": lat,
        "longitude": lon,
        "temperature_c": current["temperature_2m"],
        "relative_humidity_pct": current["relative_humidity_2m"],
        "wind_speed_kmh": current["wind_speed_10m"],
        "weather_code": current["weather_code"],
        "precipitation_mm": current["precipitation"],
        "time": current["time"],
    }

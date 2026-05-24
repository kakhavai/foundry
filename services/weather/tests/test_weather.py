import httpx
import respx
from fastapi.testclient import TestClient

from weather.main import app

client = TestClient(app)

MOCK_GEO = {
    "results": [{"name": "London", "latitude": 51.5085, "longitude": -0.1257}]
}

MOCK_WEATHER = {
    "current": {
        "time": "2026-05-23T12:00",
        "temperature_2m": 18.5,
        "relative_humidity_2m": 65,
        "wind_speed_10m": 12.3,
        "weather_code": 2,
        "precipitation": 0.0,
    }
}


@respx.mock
def test_weather_returns_conditions_for_known_location():
    respx.get("https://geocoding-api.open-meteo.com/v1/search").mock(
        return_value=httpx.Response(200, json=MOCK_GEO)
    )
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=MOCK_WEATHER)
    )
    response = client.get("/weather/London")
    assert response.status_code == 200
    data = response.json()
    assert data["location"] == "London"
    assert data["temperature_c"] == 18.5
    assert data["relative_humidity_pct"] == 65
    assert data["wind_speed_kmh"] == 12.3
    assert data["precipitation_mm"] == 0.0


@respx.mock
def test_weather_returns_404_for_unknown_location():
    respx.get("https://geocoding-api.open-meteo.com/v1/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    response = client.get("/weather/unknownxyz")
    assert response.status_code == 404


@respx.mock
def test_weather_returns_502_on_upstream_http_error():
    respx.get("https://geocoding-api.open-meteo.com/v1/search").mock(
        return_value=httpx.Response(500)
    )
    response = client.get("/weather/London")
    assert response.status_code == 502


def test_stadiums_returns_coming_soon():
    response = client.get("/weather/stadiums")
    assert response.status_code == 200
    assert response.json()["status"] == "coming_soon"

import httpx
import respx

MOCK_WEATHER = {
    "current": {
        "time": "2026-05-24T12:00",
        "temperature_2m": 18.5,
        "relative_humidity_2m": 65,
        "wind_speed_10m": 12.3,
        "weather_code": 2,
        "precipitation": 0.0,
    }
}


@respx.mock
def test_stadiums_returns_all_stadiums(client):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=MOCK_WEATHER)
    )
    response = client.get("/weather/stadiums")
    assert response.status_code == 200
    data = response.json()
    assert "stadiums" in data
    assert data["count"] == len(data["stadiums"])
    assert data["count"] > 0
    first = data["stadiums"][0]
    assert "id" in first
    assert "name" in first
    assert "team" in first
    assert "weather" in first
    assert first["weather"]["temperature_c"] == 18.5


@respx.mock
def test_stadium_weather_returns_specific_stadium(client):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=MOCK_WEATHER)
    )
    response = client.get("/weather/stadiums/arrowhead")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "arrowhead"
    assert data["name"] == "Arrowhead Stadium"
    assert data["team"] == "Kansas City Chiefs"
    assert data["weather"]["temperature_c"] == 18.5


def test_stadium_weather_returns_404_for_unknown_stadium(client):
    response = client.get("/weather/stadiums/nowhere-stadium")
    assert response.status_code == 404


@respx.mock
def test_stadiums_returns_502_when_weather_api_fails_for_single(client):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(500)
    )
    response = client.get("/weather/stadiums/lambeau")
    assert response.status_code == 502

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from weather.client import WEATHER_URL
from weather.main import app
from weather.stadiums import STADIUMS

VALID_CURRENT = {
    "current": {
        "temperature_2m": 18.0,
        "relative_humidity_2m": 62,
        "wind_speed_10m": 11.0,
        "weather_code": 1,
        "precipitation": 0.0,
        "time": "2026-09-30T14:00",
    }
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@respx.mock
def test_all_stadiums_returns_every_stadium(client):
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=VALID_CURRENT))

    body = client.get("/weather/stadiums").json()

    assert body["count"] == len(STADIUMS)
    assert len(body["stadiums"]) == len(STADIUMS)
    assert all(s["weather"] is not None for s in body["stadiums"])


@respx.mock
def test_all_stadiums_degrades_per_stadium_on_upstream_error(client):
    """One bad upstream must not fail the whole collection — weather goes null."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(503))

    body = client.get("/weather/stadiums").json()

    assert body["count"] == len(STADIUMS)
    assert all(s["weather"] is None for s in body["stadiums"])


@respx.mock
def test_all_stadiums_survives_upstream_timeout(client):
    respx.get(WEATHER_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    body = client.get("/weather/stadiums").json()

    assert body["count"] == len(STADIUMS)
    assert all(s["weather"] is None for s in body["stadiums"])


@respx.mock
def test_single_stadium_returns_502_on_upstream_error(client):
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(500))
    stadium_id = next(iter(STADIUMS))

    resp = client.get(f"/weather/stadiums/{stadium_id}")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Weather API error"


@respx.mock
def test_single_stadium_returns_502_when_upstream_unreachable(client):
    respx.get(WEATHER_URL).mock(side_effect=httpx.ConnectError("no route"))
    stadium_id = next(iter(STADIUMS))

    resp = client.get(f"/weather/stadiums/{stadium_id}")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Weather API unreachable"


def test_unknown_stadium_returns_404(client):
    resp = client.get("/weather/stadiums/not-a-real-stadium")

    assert resp.status_code == 404
    assert "not-a-real-stadium" in resp.json()["detail"]


@respx.mock
def test_concurrent_requests_are_independent(client):
    """Twenty simultaneous requests must all succeed with identical bodies."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=VALID_CURRENT))
    stadium_id = next(iter(STADIUMS))

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await asyncio.gather(
                *(ac.get(f"/weather/stadiums/{stadium_id}") for _ in range(20))
            )

    responses = asyncio.run(hammer())

    assert all(r.status_code == 200 for r in responses)
    assert len({r.text for r in responses}) == 1


def test_health_and_metrics_are_live(client):
    assert client.get("/health").json() == {"status": "ok"}

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]

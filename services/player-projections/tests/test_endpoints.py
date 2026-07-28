import pytest
from fastapi.testclient import TestClient

from player_projections.main import _state, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    _state["projections"] = []
    _state["last_updated"] = None
    _state["upstream_healthy"] = False
    yield
    _state["projections"] = []


def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_projections_empty_with_no_upstream_data():
    r = client.get("/projections")
    assert r.status_code == 200
    body = r.json()
    assert body["projections"] == []
    assert body["count"] == 0
    assert body["last_updated"] is None
    assert body["upstream_healthy"] is False


def test_projections_returns_cached_players():
    _state["projections"] = [
        {
            "id": "p_allenjosh",
            "name": "Josh Allen",
            "team": "BUF",
            "pos": "QB",
            "rank": 1,
            "proj_points": {"floor": 18.4, "expected": 32.1, "ceiling": 41.7},
        }
    ]
    _state["upstream_healthy"] = True

    r = client.get("/projections")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["projections"][0]["name"] == "Josh Allen"
    assert body["upstream_healthy"] is True

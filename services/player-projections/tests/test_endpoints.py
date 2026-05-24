import pytest
from fastapi.testclient import TestClient

from player_projections.main import app, _state

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    _state["projections"] = {}
    _state["last_updated"] = None
    _state["upstream_healthy"] = False
    yield
    _state["projections"] = {}


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
    _state["projections"] = {
        "allen-josh": {
            "id": "allen-josh",
            "name": "Josh Allen",
            "team": "BUF",
            "position": "QB",
            "projected_points": 32.1,
        }
    }
    _state["upstream_healthy"] = True

    r = client.get("/projections")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["projections"][0]["name"] == "Josh Allen"
    assert body["upstream_healthy"] is True


def test_get_projection_returns_player():
    _state["projections"] = {
        "jefferson-justin": {
            "id": "jefferson-justin",
            "name": "Justin Jefferson",
            "team": "MIN",
            "position": "WR",
            "projected_points": 26.4,
        }
    }

    r = client.get("/projections/jefferson-justin")
    assert r.status_code == 200
    assert r.json()["name"] == "Justin Jefferson"
    assert r.json()["projected_points"] == 26.4


def test_get_projection_404_for_unknown_player():
    r = client.get("/projections/nobody-unknown-xyz")
    assert r.status_code == 404
    assert r.json()["detail"] == "Player not found"

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from player_projections import main
from player_projections.main import app


@pytest.fixture(autouse=True)
def stub_mode(monkeypatch):
    """No upstream configured — the deployed default today."""
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    main._state["projections"] = []
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False
    yield
    main._state["projections"] = []
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_stub_mode_returns_empty_projections(client):
    """The documented stub-mode contract from CLAUDE.md."""
    body = client.get("/projections").json()

    assert body["projections"] == []
    assert body["count"] == 0
    assert body["upstream_healthy"] is False
    assert body["last_updated"] is None


def test_per_player_lookup_route_does_not_exist(client):
    """The API is bulk-only. A per-player route must 404 as an unknown path,
    not resolve to a handler — Task 10b deleted it deliberately."""
    resp = client.get("/projections/p_8f3a21")

    assert resp.status_code == 404
    assert "/projections/{player_id}" not in app.openapi()["paths"]


def test_populated_cache_is_served(client):
    main._state["projections"] = [
        {
            "id": "p_8f3a21",
            "name": "Deebo Samuel",
            "pos": "WR",
            "rank": 3,
            "proj_points": {"floor": 5.2, "expected": 12.4, "ceiling": 20.1},
        },
        {
            "id": "p_9a2f77",
            "name": "Baltimore",
            "pos": "DST",
            "team": "BAL",
            "yahoo_rank": 1,
            "espn_rank": 3,
        },
    ]
    main._state["upstream_healthy"] = True

    listing = client.get("/projections").json()

    assert listing["count"] == 2
    assert listing["upstream_healthy"] is True
    assert listing["projections"][0]["name"] == "Deebo Samuel"
    assert listing["projections"][0]["proj_points"]["ceiling"] == 20.1
    assert listing["projections"][1]["espn_rank"] == 3


def test_upstream_order_is_preserved(client):
    """The frontend renders ranked lanes directly from this order — it must not
    be reordered or deduplicated in transit."""
    main._state["projections"] = [{"id": f"p_{i}", "rank": i + 1} for i in range(50)]

    body = client.get("/projections").json()

    assert [p["id"] for p in body["projections"]] == [f"p_{i}" for i in range(50)]


def test_concurrent_reads_are_consistent():
    """Fifty simultaneous reads against a populated cache return identical bodies.

    Unlike the equivalent weather test, this one exercises real shared mutable
    state: `main._state` is a module-level dict read by the handler and written
    by the background poll loop.
    """
    main._state["projections"] = [{"id": f"p_{i}", "rank": i + 1} for i in range(100)]

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await asyncio.gather(*(ac.get("/projections") for _ in range(50)))

    responses = asyncio.run(hammer())

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["count"] == 100 for r in responses)
    assert len({r.text for r in responses}) == 1


def test_health_and_metrics_are_live(client):
    assert client.get("/health").json() == {"status": "ok"}

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]

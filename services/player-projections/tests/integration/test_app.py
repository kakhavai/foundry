import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from player_projections import main
from player_projections.main import app


@pytest.fixture(autouse=True)
def stub_mode(monkeypatch):
    """No upstream configured — the deployed default today."""
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", "")
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()
    yield
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()


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
    main._state["ppr"]["projections"] = [
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
    main._state["ppr"]["upstream_healthy"] = True

    listing = client.get("/projections").json()

    assert listing["count"] == 2
    assert listing["upstream_healthy"] is True
    assert listing["projections"][0]["name"] == "Deebo Samuel"
    assert listing["projections"][0]["proj_points"]["ceiling"] == 20.1
    assert listing["projections"][1]["espn_rank"] == 3


def test_upstream_order_is_preserved(client):
    """The frontend renders ranked lanes directly from this order — it must not
    be reordered or deduplicated in transit.

    Rank deliberately runs opposite to list position: a regression that sorted
    by rank would reverse the output and fail this test. With rank ascending in
    list order, such a bug would be invisible.
    """
    main._state["ppr"]["projections"] = [
        {"id": f"p_{i}", "rank": 50 - i} for i in range(50)
    ]

    body = client.get("/projections").json()

    assert [p["id"] for p in body["projections"]] == [f"p_{i}" for i in range(50)]
    assert [p["rank"] for p in body["projections"]] == list(range(50, 0, -1))


def test_concurrent_reads_are_consistent():
    """Fifty simultaneous reads against a populated cache return identical bodies.

    `main._state` is genuinely shared mutable state, but no concurrent writer
    runs here — the poll loop is not started — so this cannot currently detect
    a read/write race. It is a regression guard: reads must stay consistent,
    and if a writer is ever exercised alongside them this starts doing real
    work. Treat a failure here as a genuine concurrency bug.
    """
    main._state["ppr"]["projections"] = [
        {"id": f"p_{i}", "rank": i + 1} for i in range(100)
    ]

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await asyncio.gather(*(ac.get("/projections") for _ in range(50)))

    responses = asyncio.run(hammer())

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["count"] == 100 for r in responses)
    assert len({r.text for r in responses}) == 1


def test_stub_mode_is_empty_for_every_scoring_format(client):
    """All three caches start empty, not just the default one."""
    for fmt in main.FORMATS:
        body = client.get("/projections", params={"format": fmt}).json()
        assert body["format"] == fmt
        assert body["projections"] == []
        assert body["upstream_healthy"] is False


def test_filtered_and_unfiltered_reads_agree(client):
    """Filtering is a view over the same cache — the per-position counts must
    add back up to the unfiltered total."""
    main._state["ppr"]["projections"] = [
        {"id": f"p_{i}", "pos": pos, "rank": i + 1}
        for i, pos in enumerate(["QB", "WR", "WR", "RB", "TE", "K", "DST"])
    ]

    total = client.get("/projections").json()["count"]
    per_position = sum(
        client.get("/projections", params={"pos": pos}).json()["count"]
        for pos in main.POSITIONS
    )

    assert total == per_position == 7


def test_health_and_metrics_are_live(client):
    assert client.get("/health").json() == {"status": "ok"}

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]

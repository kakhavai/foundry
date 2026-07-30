"""Integration-level checks across the whole app — concurrency and end-to-end
wiring that a single-route unit test cannot see.

The old version of this file drove `/weather/stadiums` (deleted in Task 13)
directly against a real `weather.client` upstream client (also deleted). The
concurrency guarantee it was protecting — that concurrent requests through the
ASGI stack don't corrupt shared state — still matters, retargeted at `/signals`
reading `weather.main._state`, which is now genuinely shared mutable state
(the old routes built their own response dict per request and held nothing in
common).
"""

import asyncio

import httpx

from weather.main import app


def test_health_and_metrics_are_live(client):
    assert client.get("/health").json() == {"status": "ok"}

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]


def test_concurrent_signals_requests_are_independent(collector_token, seeded_state):
    """Twenty concurrent requests through the ASGI stack return identical bodies.

    `/signals` reads `weather.main._state`, a module-level singleton shared
    across every request — this is a genuine regression guard: if a request
    ever mutated that state instead of only reading it, this starts catching
    real races instead of passing vacuously.
    """

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {collector_token}"},
        ) as ac:
            return await asyncio.gather(*(ac.get("/signals") for _ in range(20)))

    responses = asyncio.run(hammer())

    assert all(r.status_code == 200 for r in responses)
    assert len({r.text for r in responses}) == 1

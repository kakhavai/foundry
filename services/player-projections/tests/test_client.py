import httpx
import pytest
import respx

from player_projections.client import fetch_projections

UPSTREAM_URL = "http://player-data-internal/players"

MOCK_PLAYERS = [
    {"id": "mahomes-patrick", "name": "Patrick Mahomes", "team": "KC", "position": "QB", "projected_points": 28.5},
    {"id": "hill-tyreek", "name": "Tyreek Hill", "team": "MIA", "position": "WR", "projected_points": 22.1},
]


@respx.mock
async def test_fetch_projections_returns_players():
    respx.get(UPSTREAM_URL).mock(
        return_value=httpx.Response(200, json={"players": MOCK_PLAYERS})
    )
    players = await fetch_projections(UPSTREAM_URL, "secret-key")
    assert len(players) == 2
    assert players[0]["id"] == "mahomes-patrick"


@respx.mock
async def test_fetch_projections_sends_auth_header():
    route = respx.get(UPSTREAM_URL).mock(
        return_value=httpx.Response(200, json={"players": []})
    )
    await fetch_projections(UPSTREAM_URL, "my-api-key")
    assert route.calls[0].request.headers["Authorization"] == "Bearer my-api-key"


@respx.mock
async def test_fetch_projections_raises_on_upstream_error():
    respx.get(UPSTREAM_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_projections(UPSTREAM_URL, "secret-key")


@respx.mock
async def test_fetch_projections_raises_on_network_failure():
    respx.get(UPSTREAM_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    with pytest.raises(httpx.ConnectError):
        await fetch_projections(UPSTREAM_URL, "secret-key")

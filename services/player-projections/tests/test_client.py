import httpx
import pytest
import respx

from player_projections.client import fetch_projections

S3_URL = "https://foundry-player-data.s3.amazonaws.com/projections/latest.json"

MOCK_PLAYERS = [
    {"id": "allen-josh", "name": "Josh Allen", "team": "BUF", "position": "QB", "projected_points": 32.1},
    {"id": "jefferson-justin", "name": "Justin Jefferson", "team": "MIN", "position": "WR", "projected_points": 26.4},
]


@respx.mock
async def test_fetch_projections_returns_players():
    respx.get(S3_URL).mock(
        return_value=httpx.Response(200, json={"players": MOCK_PLAYERS})
    )
    players = await fetch_projections(S3_URL)
    assert len(players) == 2
    assert players[0]["id"] == "allen-josh"


@respx.mock
async def test_fetch_projections_raises_on_upstream_error():
    respx.get(S3_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_projections(S3_URL)


@respx.mock
async def test_fetch_projections_raises_on_network_failure():
    respx.get(S3_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    with pytest.raises(httpx.ConnectError):
        await fetch_projections(S3_URL)

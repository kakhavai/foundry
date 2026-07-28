import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from player_projections.client import MalformedSnapshotError, fetch_projections

SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

URL = "https://example.test/ppr.json"


@SETTINGS
@given(body=st.one_of(st.none(), st.lists(st.integers(), max_size=3), st.integers()))
@respx.mock
async def test_non_object_snapshot_raises_typed_error(body):
    """A non-object snapshot must raise a typed error, not AttributeError."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(MalformedSnapshotError):
        await fetch_projections(URL)


@respx.mock
async def test_missing_players_key_returns_empty_list():
    """An object with no `players` key is an empty snapshot, not an error."""
    respx.get(URL).mock(return_value=httpx.Response(200, json={"format": "ppr"}))

    assert await fetch_projections(URL) == []


@SETTINGS
@given(status=st.integers(min_value=400, max_value=599))
@respx.mock
async def test_error_status_raises_http_error(status):
    respx.get(URL).mock(return_value=httpx.Response(status))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_projections(URL)


@SETTINGS
@given(size=st.integers(min_value=500, max_value=2000))
@respx.mock
async def test_large_snapshot_parses(size):
    """A snapshot far larger than production still parses without truncation.

    Real files are ~350 rows (roughly 100 per display lane); this runs well
    past that. It asserts completeness only — there is no timing assertion, so
    it says nothing about parse performance.
    """
    payload = {
        "format": "ppr",
        "players": [{"id": f"p_{i}", "rank": i + 1} for i in range(size)],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=payload))

    players = await fetch_projections(URL)

    assert len(players) == size

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from player_projections import main
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
@given(
    good=st.integers(min_value=1, max_value=5),
    bad=st.integers(min_value=1, max_value=5),
)
async def test_records_without_id_are_skipped_not_fatal(monkeypatch, good, bad):
    """One malformed record must not discard the whole batch."""
    main._state["projections"] = {}
    main._state["upstream_healthy"] = False

    players = [{"id": f"p_{i}", "name": "ok"} for i in range(good)]
    players += [{"name": "no id here"} for _ in range(bad)]

    async def fake_fetch(url):
        return players

    async def stop(_s):
        raise ImportError("stop")  # sentinel distinct from any real failure

    monkeypatch.setenv("PLAYER_DATA_URL", URL)
    monkeypatch.setattr(main, "fetch_projections", fake_fetch)
    monkeypatch.setattr(main.asyncio, "sleep", stop)

    with pytest.raises(ImportError):
        await main._poll_loop()

    assert len(main._state["projections"]) == good
    assert main._state["upstream_healthy"] is True


@SETTINGS
@given(size=st.integers(min_value=500, max_value=2000))
@respx.mock
async def test_large_snapshot_parses(size):
    """A full-league snapshot is ~500-1000 players. Parsing must not degrade."""
    payload = {
        "format": "ppr",
        "players": [{"id": f"p_{i}", "rank": i + 1} for i in range(size)],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=payload))

    players = await fetch_projections(URL)

    assert len(players) == size

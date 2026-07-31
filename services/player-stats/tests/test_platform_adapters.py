"""The two platform seams: `player-identity`'s crosswalk and `roster-scope`'s
watchlist.

Both ship switched off (`PLAYER_IDENTITY_URL` / `ROSTER_SCOPE_URL` empty), so
the stub paths below are what actually runs today and the HTTP paths are what
runs the moment a values file names either service.
"""

import httpx
import pytest
import respx

from player_stats.adapters.identity import (
    HttpIdCrosswalk,
    StubIdCrosswalk,
    UnresolvedPlayer,
    build_id_resolver,
)
from player_stats.adapters.scope import fetch_watchlist

# ── the identity crosswalk ────────────────────────────────────────────────────


async def test_the_stub_is_deterministic_across_calls():
    """A resolver that minted a new id each pass would make `revisions.py` see
    a brand-new key every capture and report the whole week as restated."""
    first = await StubIdCrosswalk().resolve("00-0034796")
    second = await StubIdCrosswalk().resolve("00-0034796")
    assert first == second
    assert first.startswith("fdy-")


async def test_the_stub_separates_two_upstream_ids():
    a = await StubIdCrosswalk().resolve("00-0034796")
    b = await StubIdCrosswalk().resolve("00-0034797")
    assert a != b


async def test_the_stub_refuses_a_blank_id_rather_than_hashing_nothing():
    """A stable id derived from nothing looks resolved to every downstream
    consumer, which is worse than an absent one."""
    with pytest.raises(UnresolvedPlayer) as caught:
        await StubIdCrosswalk().resolve("  ")
    assert caught.value.reason == "identity_missing_source_id"


async def test_build_id_resolver_defaults_to_the_stub(monkeypatch):
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "")
    async with httpx.AsyncClient() as client:
        assert isinstance(build_id_resolver(client), StubIdCrosswalk)


async def test_build_id_resolver_switches_on_the_env_var(monkeypatch):
    monkeypatch.setenv("PLAYER_IDENTITY_URL", "http://player-identity:8002")
    async with httpx.AsyncClient() as client:
        assert isinstance(build_id_resolver(client), HttpIdCrosswalk)


async def test_the_http_crosswalk_queries_by_source_and_source_id():
    """A feed that already carries a league id must not be matched by name —
    that is how two Josh Allens become one player."""
    with respx.mock:
        route = respx.get("http://player-identity:8002/resolve").mock(
            return_value=httpx.Response(
                200, json={"resolved": True, "player_id": "fdy-real", "confidence": 1.0}
            )
        )
        async with httpx.AsyncClient() as client:
            resolver = HttpIdCrosswalk(client, "http://player-identity:8002")
            assert await resolver.resolve("00-0034796") == "fdy-real"
    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["source"] == "gsis"
    assert params["source_id"] == "00-0034796"


async def test_the_http_crosswalk_caches_a_repeat_lookup():
    with respx.mock:
        route = respx.get("http://player-identity:8002/resolve").mock(
            return_value=httpx.Response(
                200, json={"resolved": True, "player_id": "fdy-real", "confidence": 1.0}
            )
        )
        async with httpx.AsyncClient() as client:
            resolver = HttpIdCrosswalk(client, "http://player-identity:8002")
            await resolver.resolve("00-0034796")
            await resolver.resolve("00-0034796")
    assert route.call_count == 1


async def test_an_unresolved_response_is_a_miss_not_a_null_id():
    with respx.mock:
        respx.get("http://player-identity:8002/resolve").mock(
            return_value=httpx.Response(
                200, json={"resolved": False, "player_id": None, "confidence": 0.0}
            )
        )
        async with httpx.AsyncClient() as client:
            resolver = HttpIdCrosswalk(client, "http://player-identity:8002")
            with pytest.raises(UnresolvedPlayer) as caught:
                await resolver.resolve("00-0034796")
    assert caught.value.reason == "identity_not_in_crosswalk"


async def test_a_low_confidence_match_is_refused():
    """A crosswalk hit scores at or near 1.0; anything lower means the lookup
    fell back to attribute scoring and must not be adopted as a published link."""
    with respx.mock:
        respx.get("http://player-identity:8002/resolve").mock(
            return_value=httpx.Response(
                200, json={"resolved": True, "player_id": "fdy-x", "confidence": 0.6}
            )
        )
        async with httpx.AsyncClient() as client:
            resolver = HttpIdCrosswalk(client, "http://player-identity:8002")
            with pytest.raises(UnresolvedPlayer) as caught:
                await resolver.resolve("00-0034796")
    assert caught.value.reason == "identity_low_confidence"


async def test_a_player_identity_outage_is_classified_not_swallowed():
    with respx.mock:
        respx.get("http://player-identity:8002/resolve").mock(
            side_effect=httpx.ConnectError("down")
        )
        async with httpx.AsyncClient() as client:
            resolver = HttpIdCrosswalk(client, "http://player-identity:8002")
            with pytest.raises(UnresolvedPlayer) as caught:
                await resolver.resolve("00-0034796")
    assert caught.value.reason == "identity_upstream_error"


# ── the roster-scope watchlist ────────────────────────────────────────────────


async def test_an_unset_url_means_no_narrowing_and_no_error(monkeypatch):
    monkeypatch.setenv("ROSTER_SCOPE_URL", "")
    async with httpx.AsyncClient() as client:
        watchlist, errors = await fetch_watchlist(client)
    assert watchlist == frozenset()
    assert errors == []


async def test_active_and_grace_slots_are_owed_but_excluded_ones_are_not(monkeypatch):
    """A player who left the depth chart on Tuesday still played on Sunday."""
    monkeypatch.setenv("ROSTER_SCOPE_URL", "http://roster-scope:8003")
    payload = {
        "players": [
            {
                "player_id": "fdy-a",
                "entity_type": "player",
                "membership_status": "active",
            },
            {
                "player_id": "fdy-b",
                "entity_type": "player",
                "membership_status": "grace",
            },
            {
                "player_id": "fdy-c",
                "entity_type": "player",
                "membership_status": "excluded",
            },
        ]
    }
    with respx.mock:
        respx.get("http://roster-scope:8003/scope/players").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as client:
            watchlist, errors = await fetch_watchlist(client)
    assert watchlist == frozenset({"fdy-a", "fdy-b"})
    assert errors == []


async def test_a_team_defense_slot_is_never_owed_a_box_score(monkeypatch):
    """32 of roster-scope's 416 slots are team defenses, which is exactly the
    difference between its universe and this collector's floor of 384."""
    monkeypatch.setenv("ROSTER_SCOPE_URL", "http://roster-scope:8003")
    payload = {
        "players": [
            {
                "player_id": "fdy-dst-kc",
                "entity_type": "team_defense",
                "membership_status": "active",
            }
        ]
    }
    with respx.mock:
        respx.get("http://roster-scope:8003/scope/players").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as client:
            watchlist, _ = await fetch_watchlist(client)
    assert watchlist == frozenset()


async def test_a_roster_scope_outage_is_recorded_not_treated_as_empty(monkeypatch):
    """An empty watchlist with no error would shrink the expectation to
    whatever the box-score feed happened to return."""
    monkeypatch.setenv("ROSTER_SCOPE_URL", "http://roster-scope:8003")
    with respx.mock:
        respx.get("http://roster-scope:8003/scope/players").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as client:
            watchlist, errors = await fetch_watchlist(client)
    assert watchlist == frozenset()
    assert len(errors) == 1
    assert errors[0]["reason"] == "scope_unavailable"

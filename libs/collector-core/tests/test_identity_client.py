import httpx
import pytest
import respx

from collector_core.identity import BATCH_LIMIT, IdentityClient, ResolveQuery

BASE = "http://player-identity:8002"


def test_batch_limit_matches_the_upstream_cap():
    """Pins the constant's value directly. The chunking test below derives
    its query count from the imported BATCH_LIMIT, so it can prove the
    chunking *logic* splits oversized batches but can never notice a change
    to the limit's *value* -- `range(0, BATCH_LIMIT + 1, BATCH_LIMIT)` always
    yields exactly two chunks no matter what BATCH_LIMIT is. This pins it to
    player-identity's own `MAX_BATCH_QUERIES` (services/player-identity/
    player_identity/resolution.py)."""
    assert BATCH_LIMIT == 500


def _result(name: str, resolved: bool, player_id=None, reason=None, candidates=None):
    return {
        "query": {
            "name": name,
            "team": None,
            "position": None,
            "source": None,
            "source_id": None,
        },
        "resolved": resolved,
        "player_id": player_id,
        "reason": reason,
        "candidates": candidates or [],
    }


@respx.mock
@pytest.mark.asyncio
async def test_resolved_queries_come_back_mapped_to_their_ids():
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_result("Patrick Mahomes", True, "fdy-abc")],
                "count": 1,
                "resolved_count": 1,
                "unresolved_count": 0,
            },
        )
    )
    query = ResolveQuery(
        name="Patrick Mahomes", team="KC", position="QB", source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([query])

    assert got == {query: "fdy-abc"}
    assert len(got) == 1


@respx.mock
@pytest.mark.asyncio
async def test_a_refusal_is_never_adopted_even_when_candidates_score_highly():
    """THE regression test for this file.

    `player-identity` returns `resolved: false` with `candidates` populated
    exactly when it has DECIDED NOT to resolve, and files the miss itself.
    A caller that re-ranks those candidates against its own floor adopts an
    identity that service explicitly refused. That bug shipped once in
    roster-scope (a local 0.5 floor let `ambiguous` at 0.97/0.93 and
    `insufficient_agreeing_attributes` at 0.667 straight through). A wrong
    player_id then propagates into an append-only lake that is never
    rewritten.
    """
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _result(
                        "A. Smith",
                        False,
                        None,
                        "ambiguous",
                        candidates=[
                            {"player_id": "fdy-x", "confidence": 0.97},
                            {"player_id": "fdy-y", "confidence": 0.93},
                        ],
                    )
                ],
                "count": 1,
                "resolved_count": 0,
                "unresolved_count": 1,
            },
        )
    )
    query = ResolveQuery(
        name="A. Smith", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([query])

    assert got == {}
    assert len(got) == 0


@respx.mock
@pytest.mark.asyncio
async def test_a_missing_resolved_field_is_not_permission():
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"query": {"name": "X"}, "player_id": "fdy-x"}],
                "count": 1,
                "resolved_count": 0,
                "unresolved_count": 1,
            },
        )
    )
    query = ResolveQuery(
        name="X", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([query])

    assert got == {}


@respx.mock
@pytest.mark.asyncio
async def test_a_truthy_but_not_true_resolved_field_is_not_permission():
    """`is True`, not truthiness. A field that is present but merely truthy
    (here `1`, not the boolean `True`) must not be read as permission either
    -- only the literal boolean `True` counts. This is the case a plain
    `if result.get("resolved")` check would let through silently, and that
    the missing-field test alone cannot distinguish, since `None is True`
    and `bool(None)` agree."""
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"query": {"name": "X"}, "resolved": 1, "player_id": "fdy-x"}
                ],
                "count": 1,
                "resolved_count": 0,
                "unresolved_count": 1,
            },
        )
    )
    query = ResolveQuery(
        name="X", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([query])

    assert got == {}


@respx.mock
@pytest.mark.asyncio
async def test_more_than_the_batch_limit_is_chunked():
    route = respx.post(f"{BASE}/resolve/batch")
    queries = [
        ResolveQuery(
            name=f"P{i}", team=None, position=None, source=None, source_id=None
        )
        for i in range(BATCH_LIMIT + 1)
    ]

    def _respond(request):
        import json as _json

        sent = _json.loads(request.content)["queries"]
        assert len(sent) <= BATCH_LIMIT, f"sent {len(sent)} > {BATCH_LIMIT}"
        return httpx.Response(
            200,
            json={
                "results": [_result(q["name"], True, f"fdy-{q['name']}") for q in sent],
                "count": len(sent),
                "resolved_count": len(sent),
                "unresolved_count": 0,
            },
        )

    route.mock(side_effect=_respond)
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many(queries)

    assert route.call_count == 2, route.call_count
    assert len(got) == BATCH_LIMIT + 1


@respx.mock
@pytest.mark.asyncio
async def test_the_bearer_token_is_sent():
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [],
                "count": 0,
                "resolved_count": 0,
                "unresolved_count": 0,
            },
        )
    )
    async with httpx.AsyncClient() as client:
        await IdentityClient(BASE, client, token="secret").resolve_many(
            [
                ResolveQuery(
                    name="X", team=None, position=None, source=None, source_id=None
                )
            ]
        )

    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"


@respx.mock
@pytest.mark.asyncio
async def test_an_empty_query_list_makes_no_request():
    route = respx.post(f"{BASE}/resolve/batch")
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([])

    assert got == {}
    assert route.call_count == 0

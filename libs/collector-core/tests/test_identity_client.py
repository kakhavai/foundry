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
    # The result list has one entry matching the single query sent -- this
    # test's purpose is the Authorization header, not the result contents,
    # but `resolve_many` now zips results against queries with strict=True
    # (see test_a_short_response_raises_rather_than_silently_dropping_the_tail),
    # so a mismatched-length mock would raise before the header is ever read.
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_result("X", True, "fdy-x")],
                "count": 1,
                "resolved_count": 1,
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
async def test_a_short_response_raises_rather_than_silently_dropping_the_tail():
    """A batch response carrying fewer results than queries sent must raise,
    not silently drop the tail.

    Without `strict=True` on the zip, a short response reads as if the
    missing players were simply unresolved: they land in `coverage.missing`
    like any other miss, and nothing anywhere flags that the reply itself
    was malformed. That is the same failure shape as a truncated upstream
    response elsewhere in this project -- a partial answer must be loud, not
    indistinguishable from a normal miss.
    """
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_result("A", True, "fdy-a")],
                "count": 1,
                "resolved_count": 1,
                "unresolved_count": 0,
            },
        )
    )
    queries = [
        ResolveQuery(name="A", team=None, position=None, source=None, source_id=None),
        ResolveQuery(name="B", team=None, position=None, source=None, source_id=None),
    ]
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await IdentityClient(BASE, client, token="t").resolve_many(queries)


@respx.mock
@pytest.mark.asyncio
async def test_an_empty_query_list_makes_no_request():
    route = respx.post(f"{BASE}/resolve/batch")
    async with httpx.AsyncClient() as client:
        got = await IdentityClient(BASE, client, token="t").resolve_many([])

    assert got == {}
    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_a_repeated_query_is_not_re_requested():
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_result("P", True, "fdy-p")],
                "count": 1,
                "resolved_count": 1,
                "unresolved_count": 0,
            },
        )
    )
    query = ResolveQuery(
        name="P", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t")
        first = await identity.resolve_many([query])
        second = await identity.resolve_many([query])

    assert first == second == {query: "fdy-p"}
    assert route.call_count == 1, route.call_count


@respx.mock
@pytest.mark.asyncio
async def test_an_unresolved_query_is_retried_rather_than_cached_as_a_refusal():
    """A miss may become a hit when the crosswalk is republished mid-season.
    Caching the refusal would pin the gap until a restart."""
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_result("P", False, None, "ambiguous")],
                "count": 1,
                "resolved_count": 0,
                "unresolved_count": 1,
            },
        )
    )
    query = ResolveQuery(
        name="P", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t")
        await identity.resolve_many([query])
        await identity.resolve_many([query])

    assert route.call_count == 2, route.call_count


@respx.mock
@pytest.mark.asyncio
async def test_an_unreachable_upstream_does_not_raise():
    """THE regression test for the missing error contract.

    The design spec: 'player-identity unreachable -> affected players
    resolve to nothing; each lands in coverage.missing with reason
    identity_unresolved. The pass still writes an envelope.' A bare
    `raise_for_status()` used to propagate `httpx.HTTPStatusError` straight
    out of `resolve_many`, which would take a collector's whole capture pass
    down on a single 503 instead of a classified partial.
    """
    respx.post(f"{BASE}/resolve/batch").mock(return_value=httpx.Response(503))
    query = ResolveQuery(
        name="X", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t")
        got = await identity.resolve_many([query])

    assert got == {}
    assert identity.failures == {query: identity.failures[query]}
    assert "identity_upstream_error" in identity.failures[query]


@respx.mock
@pytest.mark.asyncio
async def test_a_connection_error_does_not_raise():
    respx.post(f"{BASE}/resolve/batch").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    query = ResolveQuery(
        name="X", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t")
        got = await identity.resolve_many([query])

    assert got == {}
    assert query in identity.failures


@respx.mock
@pytest.mark.asyncio
async def test_a_failed_chunk_does_not_discard_an_earlier_successful_chunk():
    """A batch spanning two chunks where only the second fails must not lose
    what the first already resolved -- the exact shape of the review's
    failure scenario: chunks 1-2 resolve, chunk 3 gets a 503."""
    route = respx.post(f"{BASE}/resolve/batch")
    calls = {"n": 0}

    def _respond(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _result(f"P{i}", True, f"fdy-{i}") for i in range(BATCH_LIMIT)
                    ],
                    "count": BATCH_LIMIT,
                    "resolved_count": BATCH_LIMIT,
                    "unresolved_count": 0,
                },
            )
        return httpx.Response(503)

    route.mock(side_effect=_respond)

    queries = [
        ResolveQuery(
            name=f"P{i}", team=None, position=None, source=None, source_id=None
        )
        for i in range(BATCH_LIMIT)
    ]
    straggler = ResolveQuery(
        name="Straggler", team=None, position=None, source=None, source_id=None
    )
    queries.append(straggler)

    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t")
        got = await identity.resolve_many(queries)

    assert len(got) == BATCH_LIMIT
    assert route.call_count == 2
    assert identity.failures == {straggler: identity.failures[straggler]}


@respx.mock
@pytest.mark.asyncio
async def test_failures_reset_on_each_call_even_when_the_new_call_succeeds():
    route = respx.post(f"{BASE}/resolve/batch")
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(
                200,
                json={
                    "results": [_result("X", True, "fdy-x")],
                    "count": 1,
                    "resolved_count": 1,
                    "unresolved_count": 0,
                },
            ),
        ]
    )
    route.mock(side_effect=lambda request: next(responses))
    query = ResolveQuery(
        name="X", team=None, position=None, source=None, source_id=None
    )
    async with httpx.AsyncClient() as client:
        identity = IdentityClient(BASE, client, token="t")
        await identity.resolve_many([query])
        assert identity.failures, "first call failed and must be recorded"
        await identity.resolve_many([query])
        assert identity.failures == {}, "a clean call must clear the prior failure"


@respx.mock
@pytest.mark.asyncio
async def test_a_malformed_response_still_raises_rather_than_being_swallowed():
    """The failure-handling added for an unreachable upstream must not widen
    to cover a `player-identity` contract violation too -- a response with
    the wrong shape is a bug in that service, not a reachability problem,
    and must keep raising."""
    respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_result("A", True, "fdy-a")],
                "count": 1,
                "resolved_count": 1,
                "unresolved_count": 0,
            },
        )
    )
    queries = [
        ResolveQuery(name="A", team=None, position=None, source=None, source_id=None),
        ResolveQuery(name="B", team=None, position=None, source=None, source_id=None),
    ]
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await IdentityClient(BASE, client, token="t").resolve_many(queries)


def test_resolve_query_carries_jersey_number_and_season():
    """`player-identity`'s `build_query` reads both straight off the request
    body and `resolution.WEIGHTS` scores `jersey_number` at parity with
    `team` -- omitting either would resolve a collector on this seam worse
    than `roster-scope`'s own single-query resolver, which already sends
    `jersey_number` today."""
    query = ResolveQuery(name="X", jersey_number=87, season=2026)
    assert query.jersey_number == 87
    assert query.season == 2026


@respx.mock
@pytest.mark.asyncio
async def test_jersey_number_and_season_are_sent_on_the_wire():
    route = respx.post(f"{BASE}/resolve/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [_result("X", True, "fdy-x")],
                "count": 1,
                "resolved_count": 1,
                "unresolved_count": 0,
            },
        )
    )
    query = ResolveQuery(
        name="X",
        team=None,
        position=None,
        jersey_number=87,
        season=2026,
        source=None,
        source_id=None,
    )
    async with httpx.AsyncClient() as client:
        await IdentityClient(BASE, client, token="t").resolve_many([query])

    import json as _json

    sent = _json.loads(route.calls[0].request.content)["queries"][0]
    assert sent["jersey_number"] == 87
    assert sent["season"] == 2026

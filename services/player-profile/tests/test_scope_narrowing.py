"""Narrowing, proven behaviourally rather than structurally.

`tests/test_scope_aware_gate.py` at the repo root reads this collector's source
by AST and fails if `scope_aware: true` is declared without importing
`collector_core.scope.ScopeClient`. That proves the seam is *wired in*. It
cannot prove the seam is used *correctly* — a collector can import `ScopeClient`
and still call it after the fetch, or fall back to fetching everyone, or adopt
an id `player-identity` refused. Those three are what this file is for.
"""

import httpx
import pytest
import respx
from collector_core.scope import ScopeUnavailable

from player_profile.adapters import upstream as upstream_mod
from player_profile.adapters.scope import (
    TEAM_DEFENSE_PREFIX,
    individual_players,
)
from player_profile.capture import (
    BIOGRAPHICAL,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    capture_player_profile,
)

from .conftest import (
    CANONICAL_IDS,
    NOW,
    OUT_OF_SCOPE_GSIS,
    SCOPED_IDS,
    SEASON,
    TEAM_DEFENSE_IDS,
    WEEK,
    SpyLake,
    mock_identity,
    mock_upstreams,
    scope_envelope,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_profile(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def _upstream_routes():
    """Every URL this collector could possibly fetch, as respx routes.

    Returned so a test can assert `call_count == 0` across all of them, which is
    the only way to prove "zero upstream calls" rather than "the one URL I
    remembered to check was not called".
    """
    routes = [
        respx.mock.get(upstream_mod.PLAYERS_URL),
        respx.mock.get(upstream_mod.COMBINE_URL),
    ]
    for season in range(upstream_mod.CAREER_SNAP_FIRST_SEASON, SEASON + 1):
        routes.append(
            respx.mock.get(upstream_mod.SNAP_COUNTS_URL.format(season=season))
        )
    for route in routes:
        route.respond(200, text="")
    return routes


# ── failing closed ───────────────────────────────────────────────────────────


@respx.mock
async def test_an_unavailable_scope_makes_zero_upstream_calls():
    """The whole point of resolving the scope FIRST.

    An unnarrowed fallback would spend the entire per-run budget — ~44 MB
    across three feeds — precisely in the run where the fleet's own scope is
    unavailable, turning a `roster-scope` incident into a fleet-wide one.
    """
    mock_identity(respx.mock)
    routes = _upstream_routes()
    lake = SpyLake()  # deliberately empty: no scope has ever been published

    with pytest.raises(ScopeUnavailable):
        await capture(lake)

    assert [route.call_count for route in routes] == [0] * len(routes)


@respx.mock
async def test_an_unavailable_scope_writes_a_present_zero_envelope_and_reraises():
    """ "We failed" and "we never tried" are different facts, and a gap in an
    append-only lake must be explicit rather than inferred from absence.

    The re-raise matters as much as the write: `run_capture_loop` catches it and
    leaves `CaptureState` untouched, so the last good capture survives.
    """
    mock_identity(respx.mock)
    _upstream_routes()
    lake = SpyLake()

    with pytest.raises(ScopeUnavailable):
        await capture(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        reasons = {error["reason"] for error in envelope.errors}
        assert "scope_unavailable" in reasons, reasons


@respx.mock
async def test_an_empty_scope_is_named_scope_empty_not_scope_unavailable():
    """A `roster-scope` capture against a dead upstream still writes an
    envelope, and an empty one is a different incident from a missing one.

    Collapsing the two costs an operator the only thing the envelope could have
    told them.
    """
    mock_identity(respx.mock)
    _upstream_routes()
    lake = SpyLake()
    lake.write(scope_envelope(player_ids=[], include_team_defenses=False))

    with pytest.raises(ScopeUnavailable) as raised:
        await capture(lake)

    assert raised.value.reason == "scope_empty"


@respx.mock
async def test_no_player_identity_url_fails_closed_with_its_own_reason():
    """A scope with no identity seam to join through narrows to nothing just as
    completely as no scope does — and fetching 7.3 MB to publish zero rows is
    exactly the waste failing closed exists to prevent.

    Its own reason, because "roster-scope published nothing" and "this pod was
    never pointed at player-identity" are different fixes.
    """
    _upstream_routes()
    lake = SpyLake()
    lake.write(scope_envelope())

    with respx.mock:
        with pytest.raises(ScopeUnavailable) as raised:
            import os

            os.environ["PLAYER_IDENTITY_URL"] = ""
            try:
                await capture(lake)
            finally:
                os.environ.pop("PLAYER_IDENTITY_URL", None)

    assert raised.value.reason == "identity_unavailable"


@respx.mock
async def test_no_player_identity_url_makes_zero_upstream_calls():
    """The identity refusal has to be resolved in the same place the scope is,
    or it fails closed AFTER paying for the fetch."""
    routes = _upstream_routes()
    lake = SpyLake()
    lake.write(scope_envelope())

    import os

    os.environ["PLAYER_IDENTITY_URL"] = ""
    try:
        with pytest.raises(ScopeUnavailable):
            await capture(lake)
    finally:
        os.environ.pop("PLAYER_IDENTITY_URL", None)

    assert [route.call_count for route in routes] == [0] * len(routes)


# ── narrowing ────────────────────────────────────────────────────────────────


@respx.mock
async def test_a_resolved_row_outside_the_scope_is_dropped(lake):
    """The narrowing itself. Foxtrot resolves cleanly and is deliberately absent
    from the watchlist, so a collector that published him would be publishing
    the whole league."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    published = {row["player_id"] for row in envelopes[BIOGRAPHICAL].signals}
    assert CANONICAL_IDS[OUT_OF_SCOPE_GSIS] not in published
    assert published == set(SCOPED_IDS)


@respx.mock
async def test_a_dropped_out_of_scope_row_is_not_a_coverage_miss(lake):
    """An out-of-scope player was never owed, so recording one would make
    narrowing read as a permanent coverage regression that buries the rows that
    genuinely failed."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    missing = envelopes[BIOGRAPHICAL].coverage.missing
    assert not any(CANONICAL_IDS[OUT_OF_SCOPE_GSIS] in key for key in missing)


@respx.mock
async def test_a_refused_row_is_dropped_rather_than_published(lake):
    """`player-identity` populates `candidates` precisely when it has REFUSED.

    A client that re-ranks them adopts an identity it was explicitly declined.
    The fixture's refusals come back with a `fdy-decoy00000` candidate for
    exactly this reason.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock, resolvable={})

    envelopes = await capture(lake)

    published = {row["player_id"] for row in envelopes[BIOGRAPHICAL].signals}
    assert published == set()


@respx.mock
async def test_a_raw_upstream_id_is_never_adopted_for_a_refused_row():
    """The `.get(query)` default, isolated — and it needs isolating.

    A scope of canonical `fdy-` ids filters a raw GSIS id out **positionally**:
    `00-0000002` cannot collide with an `fdy-` member, so a collector that fell
    back to `resolved.get(query, row.gsis_id)` would still publish nothing and
    every ordinary narrowing test would stay green. Mutation testing is what
    caught that; this is the test that closes it.

    So the scope here deliberately CONTAINS the raw upstream key. Now the only
    thing standing between a refused row and a published one is the absence of a
    default on that `.get`.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock, resolvable={})
    lake = SpyLake()
    lake.write(
        scope_envelope(
            player_ids=[*SCOPED_IDS, "00-0000002"], include_team_defenses=False
        )
    )

    envelopes = await capture(lake)

    published = {row["player_id"] for row in envelopes[BIOGRAPHICAL].signals}
    assert "00-0000002" not in published, (
        "a row player-identity refused was published under the feed's own GSIS "
        "key — resolved.get(query) must have no default"
    )
    assert published == set()


@respx.mock
async def test_the_scope_is_read_from_the_lake_not_over_http(lake):
    """`roster-scope`'s HTTP routes exist for the out-of-repo generator and for
    operators. A collector asks a different question — "what was the last scope
    this fleet agreed on" — and the lake is the answer that survives a
    `roster-scope` outage."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    roster_scope_http = respx.mock.get(url__regex=r".*roster-scope.*").respond(200)

    await capture(lake)

    assert roster_scope_http.call_count == 0


# ── the team-defense exclusion ───────────────────────────────────────────────


@respx.mock
async def test_resolution_goes_through_the_shared_seam_and_is_chunked():
    """`player-identity` caps `POST /resolve/batch` at 500 and answers 422 above
    it, so a collector that posted the whole feed in one body would break the
    moment its feed outgrew the cap.

    The chunking itself is `IdentityClient.resolve_many`'s, not this collector's
    — which is the point. This asserts the shared seam is what reaches the wire,
    so a hand-rolled POST here would fail even though it would look correct in
    every other test. `tests/test_identity_batch_limit.py` at the repo root pins
    the constant against `player-identity`'s own, since the two cannot import
    each other.
    """
    from collector_core.identity import BATCH_LIMIT

    from .conftest import FIXTURE_PLAYERS, players_csv

    # BATCH_LIMIT + 10 upstream rows, so exactly two chunks are required.
    many = []
    for index in range(BATCH_LIMIT + 10):
        record = list(FIXTURE_PLAYERS[1])
        record[0] = f"00-{index:07d}"
        record[1] = f"Pfr{index:05d}"
        many.append(record)
    mock_upstreams(respx.mock, players=players_csv(many))
    resolve = respx.mock.post("http://player-identity.test/resolve/batch").mock(
        side_effect=_empty_resolutions
    )
    lake = SpyLake()
    lake.write(scope_envelope())

    await capture(lake)

    assert resolve.call_count == 2
    sizes = [
        len(__import__("json").loads(call.request.content)["queries"])
        for call in resolve.calls
    ]
    assert sizes == [BATCH_LIMIT, 10]


def _empty_resolutions(request):
    """Refuse every query, with candidates — the shape a real refusal has."""
    import json

    count = len(json.loads(request.content)["queries"])
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "resolved": False,
                    "player_id": None,
                    "confidence": 0.4,
                    "candidates": [{"player_id": "fdy-decoy00000", "score": 0.4}],
                }
            ]
            * count
        },
    )


def test_individual_players_excludes_team_defenses_and_keeps_everyone_else():
    """Both directions, because a predicate that excludes everything would pass
    the first assertion alone."""
    scope = type("S", (), {"members": frozenset([*SCOPED_IDS, *TEAM_DEFENSE_IDS])})()

    owed = individual_players(scope)

    assert owed == frozenset(SCOPED_IDS)
    assert len(owed) == len(SCOPED_IDS)
    assert all(not pid.startswith(TEAM_DEFENSE_PREFIX) for pid in owed)

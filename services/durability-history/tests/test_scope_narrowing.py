"""Narrowing, proven behaviourally rather than structurally.

`tests/test_scope_aware_gate.py` at the repo root reads this collector's source
by AST and fails if `scope_aware: true` is declared without importing
`collector_core.scope.ScopeClient`. That proves the seam is *wired in*. It cannot
prove the seam is used *correctly* — a collector can import `ScopeClient` and
still call it after the fetch, or fall back to fetching everyone, or adopt an id
`player-identity` refused. Those three are what this file is for.

The cost here is why it matters: the five feeds are 43.8 MB on a cold window, and
an unnarrowed fallback would spend the whole of it precisely in the run where the
fleet's own scope is unavailable.
"""

import os
from datetime import UTC, datetime

import httpx
import pytest
import respx
from collector_core.scope import Scope, ScopeUnavailable

from durability_history.adapters import upstream as upstream_mod
from durability_history.adapters.scope import TEAM_DEFENSE_PREFIX, individual_players
from durability_history.capture import (
    EXPECTED_FLOOR,
    INJURY_HISTORY,
    SIGNAL_TYPES,
    capture_durability_history,
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
    games_csv,
    mock_identity,
    mock_upstreams,
    players_csv,
    scope_envelope,
    upstream_urls,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def _all_upstream_routes():
    """EVERY URL this collector could possibly fetch, as respx routes.

    Returned so a test can assert `call_count == 0` across all of them, which is
    the only way to prove "zero upstream calls" rather than "the one URL I
    remembered to check was not called".
    """
    routes = [respx.mock.get(url) for url in upstream_urls()]
    for route in routes:
        route.respond(200, text="")
    return routes


def _own_writes(lake):
    return [e for e in lake.writes if e.collector == "durability-history"]


# ── failing closed ───────────────────────────────────────────────────────────


@respx.mock
async def test_an_unavailable_scope_makes_zero_upstream_calls():
    """The whole point of resolving the scope FIRST.

    An unnarrowed fallback would spend the entire per-run budget — 43.8 MB across
    five feeds — precisely in the run where the fleet's own scope is unavailable,
    turning a `roster-scope` incident into a fleet-wide one.
    """
    mock_identity(respx.mock)
    routes = _all_upstream_routes()
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
    _all_upstream_routes()
    lake = SpyLake()

    with pytest.raises(ScopeUnavailable):
        await capture(lake)

    written = _own_writes(lake)
    assert {e.signal_type for e in written} == set(SIGNAL_TYPES)
    for envelope in written:
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        reasons = {error["reason"] for error in envelope.errors}
        assert "scope_unavailable" in reasons, reasons


@respx.mock
async def test_an_empty_scope_is_named_scope_empty_not_scope_unavailable():
    """A `roster-scope` capture against a dead upstream still writes an envelope,
    and an empty one is a different incident from a missing one. Collapsing the
    two costs an operator the only thing the envelope could have told them."""
    mock_identity(respx.mock)
    _all_upstream_routes()
    lake = SpyLake()
    lake.write(scope_envelope(player_ids=[], include_team_defenses=False))

    with pytest.raises(ScopeUnavailable) as raised:
        await capture(lake)

    assert raised.value.reason == "scope_empty"


@respx.mock
async def test_no_player_identity_url_fails_closed_with_its_own_reason():
    """A scope with no identity seam to join through narrows to nothing just as
    completely as no scope does — and fetching 43.8 MB to publish zero rows is
    exactly the waste failing closed exists to prevent.

    Its own reason, because "roster-scope published nothing" and "this pod was
    never pointed at player-identity" are different fixes.
    """
    _all_upstream_routes()
    lake = SpyLake()
    lake.write(scope_envelope())

    os.environ["PLAYER_IDENTITY_URL"] = ""
    try:
        with pytest.raises(ScopeUnavailable) as raised:
            await capture(lake)
    finally:
        os.environ.pop("PLAYER_IDENTITY_URL", None)

    assert raised.value.reason == "identity_unavailable"


@respx.mock
async def test_no_player_identity_url_makes_zero_upstream_calls():
    """The identity refusal has to be resolved in the same place the scope is, or
    it fails closed AFTER paying for the fetch."""
    routes = _all_upstream_routes()
    lake = SpyLake()
    lake.write(scope_envelope())

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
    from the watchlist, so a collector that published him would be publishing the
    whole league."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    published = {row["player_id"] for row in envelopes[INJURY_HISTORY].signals}
    assert CANONICAL_IDS[OUT_OF_SCOPE_GSIS] not in published
    assert published == set(SCOPED_IDS)


@respx.mock
async def test_the_per_season_feeds_are_filtered_to_the_resolved_scope(lake):
    """Narrowing is not only about which rows are published — it is what keeps
    the 8.28 MB weekly-stats file from materialising the ~4,600 players this
    collector does not want.

    Proven through the reconstruction rather than by inspecting a private cache:
    Foxtrot has snap and stat rows in every fixture document, and none of them
    reaches any published row.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        for row in envelope.signals:
            assert row["player_id"] != CANONICAL_IDS[OUT_OF_SCOPE_GSIS]


@respx.mock
async def test_an_identity_refusal_is_never_adopted_as_a_raw_id(lake):
    """`resolved: false` comes back WITH candidates, which is the shape
    `player-identity` uses when it has deliberately refused. A caller that
    re-ranks those adopts an identity it was declined — and here that means
    attributing one player's injury history to another."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock, resolvable={})

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        assert envelope.signals == []
        assert envelope.coverage.present == 0
    published = {row.get("player_id") for e in envelopes.values() for row in e.signals}
    assert "fdy-decoy00000" not in published


async def test_an_unresolved_row_is_dropped_even_when_its_RAW_id_is_in_scope():
    """The defaulting `.get` bug, pinned where it is actually observable.

    `resolved.get(query, row.gsis_id)` is **positionally safe**: the adopted raw
    id is then checked against a scope of `fdy-` ids, fails, and the row is
    dropped anyway — so it survives every end-to-end narrowing test while
    silently adopting upstream ids. Mutation testing on this collector confirmed
    exactly that; it survived nine capture-level narrowing assertions.

    The only way to observe it is a scope that CONTAINS the raw upstream id. That
    is not a shape `roster-scope` produces, and it is not meant to be: it is the
    controlled condition under which "we read only player-identity's answer" and
    "we fall back to the feed's own key" stop agreeing.
    """
    from collector_core.identity import IdentityClient

    from durability_history.adapters.scope import IdentityFailures, resolve_in_scope
    from durability_history.adapters.upstream import PlayerRow

    row = PlayerRow(
        gsis_id="00-0000042",
        pfr_id="RawPl000",
        display_name="Raw Player",
        team="SEA",
        position="RB",
        jersey_number=21,
        birth_date=None,
        rookie_season=2024,
    )

    class RefusingIdentity(IdentityClient):
        def __init__(self):
            self.failures = {}

        async def resolve_many(self, queries):
            # Exactly `player-identity`'s refusal shape: nothing returned, with
            # candidates filed server-side. `resolve_many` omits refused queries.
            return {}

    kept = await resolve_in_scope(
        [row],
        season=SEASON,
        # The raw upstream key, deliberately placed in the scope.
        scope_members=frozenset({"00-0000042"}),
        identity=RefusingIdentity(),
        failures=IdentityFailures(),
    )

    assert kept == [], (
        "an id player-identity refused was published under the feed's own GSIS "
        "key — this is the defaulting `.get(query, row.gsis_id)`"
    )


@respx.mock
async def test_zero_resolved_rows_costs_zero_PER_SEASON_fetches(lake):
    """The narrowing decision one layer below the scope check.

    With nothing to filter FOR, the three per-season sweeps would download ~34 MB
    and keep none of it — and a total `player-identity` outage is precisely when
    that happens, so without the guard an identity incident costs the full budget
    on every pass. That is the cascade `fetch_scope_or_fail` exists to prevent,
    arriving by a different route.

    `games.csv` and `players.csv` are still fetched: resolution needs them, so
    they are the price of discovering there is nothing to resolve.
    """
    mock_identity(respx.mock, resolvable={})
    per_season = [
        respx.mock.get(url)
        for url in upstream_urls()
        if url not in {upstream_mod.GAMES_URL, upstream_mod.PLAYERS_URL}
    ]
    for route in per_season:
        route.respond(200, text="")
    respx.mock.get(upstream_mod.GAMES_URL).respond(200, text=games_csv())
    respx.mock.get(upstream_mod.PLAYERS_URL).respond(200, text=players_csv())

    envelopes = await capture(lake)

    assert per_season, "the URL list is empty; this test asserts nothing"
    assert [route.call_count for route in per_season] == [0] * len(per_season)
    for envelope in envelopes.values():
        assert envelope.coverage.present == 0


@respx.mock
async def test_an_identity_outage_is_distinguishable_from_a_short_week(lake):
    """A dead `player-identity` and a genuinely small scope produce the same
    empty envelope unless `IdentityClient.failures` is read. Without this, three
    incidents share one symptom: `below_expected_floor` and nothing else."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock, status=503)

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        reasons = {error["reason"] for error in envelope.errors}
        assert "identity_upstream_error" in reasons, reasons


@respx.mock
async def test_a_team_defense_is_not_owed_a_durability_record(lake):
    """`roster-scope` mints one per club and they have no hamstrings. Expecting
    them would peg the coverage ratio 32 slots short forever."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        assert not any(
            key.startswith(f"player:{TEAM_DEFENSE_PREFIX}")
            for key in envelope.coverage.missing
        )


def test_individual_players_excludes_team_defenses():
    scope = scope_envelope()
    members = frozenset(row["player_id"] for row in scope.signals)
    resolved = individual_players(
        Scope(
            members=members,
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            signal_type="scope_membership_weekly",
        )
    )
    assert resolved == frozenset(SCOPED_IDS)
    assert not resolved & frozenset(TEAM_DEFENSE_IDS)


@respx.mock
async def test_a_week_rollover_falls_back_to_the_previous_weeks_scope():
    """`ScopeClient` falls back to `week - 1`, which is what stops every week
    rollover failing this collector closed until roster-scope's weekly capture
    lands. Proven here because a collector that hand-rolled the read would not
    inherit it."""
    lake = SpyLake()
    lake.write(scope_envelope(week=WEEK - 1))
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert envelopes[INJURY_HISTORY].signals


def test_the_scope_seam_is_the_shared_one_not_a_local_reimplementation():
    """`ScopeClient` reads the LAKE, never `roster-scope` over HTTP — the last
    good scope has to survive a `roster-scope` outage."""
    source = upstream_mod.__file__.replace(
        "adapters\\upstream.py", "adapters\\scope.py"
    ).replace("adapters/upstream.py", "adapters/scope.py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "from collector_core.scope import" in text
    assert "ScopeClient" in text
    assert "roster-scope.default.svc" not in text
    assert "/scope/players" not in text

"""Narrowing and identity, proven behaviourally rather than structurally.

`tests/test_scope_aware_gate.py` at the repo root reads this collector's source
by AST and fails if `scope_aware: true` is declared without importing
`collector_core.scope.ScopeClient`. That proves the seam is *wired in*. It
cannot prove the seam is used *correctly* — a collector can import `ScopeClient`
and still call it after the fetch, or fall back to fetching everyone, or adopt
an id `player-identity` refused. Those three are what this file is for.

Identity carries extra weight here because **the upstream keys players by
NAME**. Every other scope-aware collector in the fleet joins on a published
crosswalk id, which `player-identity` adopts at Tier 1 with no scoring. This one
lands in Tier 3 weighted agreement, so a wrong club, an unknown position code
and a defaulting `.get` each cost something different and none of them raises.
"""

import json
import os

import httpx
import pytest
import respx
from collector_core.identity import IdentityClient
from collector_core.scope import Scope, ScopeUnavailable

from player_contract.adapters import upstream as upstream_mod
from player_contract.adapters.scope import (
    TEAM_DEFENSE_PREFIX,
    IdentityFailures,
    individual_players,
    resolve_in_scope,
)
from player_contract.capture import (
    CONTRACT_STATUS,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    capture_player_contract,
)

from .conftest import (
    CANONICAL_IDS,
    IDENTITY_URL,
    NOW,
    OUT_OF_SCOPE_NAME,
    SCOPED_IDS,
    SEASON,
    TEAM_DEFENSE_IDS,
    WEEK,
    SpyLake,
    contracts_parquet,
    mock_identity,
    mock_upstream,
    row,
    scope_envelope,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_contract(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def _all_upstream_routes():
    """EVERY URL this collector could possibly fetch, as respx routes.

    A list of one today. Returned as a list anyway so a `call_count == 0`
    assertion cannot be satisfied by checking only the one URL a test happened
    to remember, and so adding a second feed later does not silently narrow the
    assertion.
    """
    routes = [respx.mock.get(url) for url in (upstream_mod.UPSTREAM_URL,)]
    for route in routes:
        route.respond(200, content=b"")
    return routes


def _own_writes(lake):
    return [e for e in lake.writes if e.collector == "player-contract"]


# ── failing closed ───────────────────────────────────────────────────────────


@respx.mock
async def test_an_unavailable_scope_makes_zero_upstream_calls():
    """The whole point of resolving the scope FIRST.

    An unnarrowed fallback would spend the pass's budget precisely in the run
    where the fleet's own scope is unavailable, turning a `roster-scope`
    incident into a fleet-wide one. The bytes are modest here; the ordering is
    the invariant, and it is the one that gets moved by a refactor that looks
    harmless.
    """
    mock_identity(respx.mock)
    routes = _all_upstream_routes()
    lake = SpyLake()  # deliberately empty: no scope has ever been published

    with pytest.raises(ScopeUnavailable):
        await capture(lake)

    assert routes, "the URL list is empty; this test asserts nothing"
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
    completely as no scope does — and here more completely than anywhere else in
    the fleet, because the feed carries no id this collector could fall back to.

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
    """The identity refusal has to be resolved in the same place the scope is,
    or it fails closed AFTER paying for the fetch."""
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


@respx.mock
async def test_a_lake_that_fails_outright_still_writes_a_failure_envelope():
    """`ScopeClient`'s read is I/O and the lake can fail *outright* rather than
    answering empty. Without `fetch_scope_or_fail`'s second `except` arm that
    escapes the capture coroutine entirely — no envelope, no failure counter,
    just a log line from whoever dispatched the pass."""
    mock_identity(respx.mock)
    routes = _all_upstream_routes()

    class BrokenLake(SpyLake):
        def list_keys(self, *args, **kwargs):
            raise RuntimeError("object store unreachable")

    lake = BrokenLake()

    with pytest.raises(RuntimeError):
        await capture(lake)

    assert [route.call_count for route in routes] == [0] * len(routes)
    written = _own_writes(lake)
    assert {e.signal_type for e in written} == set(SIGNAL_TYPES)
    assert all(e.coverage.present == 0 for e in written)


# ── narrowing ────────────────────────────────────────────────────────────────


@respx.mock
async def test_a_resolved_row_outside_the_scope_is_dropped(lake):
    """The narrowing itself. Foxtrot resolves cleanly and is deliberately absent
    from the watchlist, so a collector that published him would be publishing
    the whole league — 2,930 contracts instead of 384."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    published = {row["player_id"] for row in envelopes[CONTRACT_STATUS].signals}
    assert published, "nothing was published; the narrowing assertion is vacuous"
    assert CANONICAL_IDS[OUT_OF_SCOPE_NAME] not in published
    assert published <= set(SCOPED_IDS)


@respx.mock
async def test_an_identity_refusal_is_never_adopted_as_a_raw_id(lake):
    """`resolved: false` comes back WITH candidates, which is the shape
    `player-identity` uses when it has deliberately refused. A caller that
    re-ranks those adopts an identity it was declined — and here that means
    attributing one player's guarantee and contract year to another."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock, resolvable={})

    envelopes = await capture(lake)

    envelope = envelopes[CONTRACT_STATUS]
    assert envelope.signals == []
    assert envelope.coverage.present == 0
    assert "fdy-decoy00000" not in str(envelope.to_dict())


async def test_an_unresolved_row_is_dropped_even_when_its_RAW_id_is_in_scope():
    """The defaulting `.get` bug, pinned where it is actually observable.

    `resolved.get(query, row.player_name)` is **positionally safe**: the adopted
    raw string is then checked against a scope of `fdy-` ids, fails, and the row
    is dropped anyway — so it survives every end-to-end narrowing test while
    silently making raw upstream strings eligible to become canonical ids. The
    only way to observe it is a scope that CONTAINS the raw upstream key.

    That is not a shape `roster-scope` produces and is not meant to be: it is
    the controlled condition under which "we read only player-identity's answer"
    and "we fall back to the feed's own key" stop agreeing.

    Both plausible defaults are covered, because the feed carries two candidate
    raw keys — the display name and `otc_id` — and a test naming only one leaves
    the other alive.
    """
    raw_row = upstream_mod.ContractRow(
        otc_id="4242",
        gsis_id="00-0004242",
        player_name="Raw Player",
        otc_position="RB",
        otc_team="Seahawks",
        year_signed=2025,
        years=3,
        total_value_usd=1,
        guaranteed_total_usd=1,
        cap_hit_current_usd=1,
        signing_bonus_proration_usd=1,
    )

    class RefusingIdentity(IdentityClient):
        def __init__(self):
            self.failures = {}

        async def resolve_many(self, queries):
            # Exactly `player-identity`'s refusal shape: nothing returned, with
            # candidates filed server-side. `resolve_many` omits refused queries.
            return {}

    kept = await resolve_in_scope(
        [raw_row],
        season=SEASON,
        # Both raw upstream keys, deliberately placed in the scope.
        scope_members=frozenset({"Raw Player", "4242", "00-0004242"}),
        identity=RefusingIdentity(),
        failures=IdentityFailures(),
    )

    assert kept == [], (
        "an id player-identity refused was published under one of the feed's "
        "own keys — this is the defaulting `.get(query, <raw>)`"
    )


@respx.mock
async def test_an_identity_outage_is_distinguishable_from_a_short_week(lake):
    """A dead `player-identity` and a genuinely small scope produce the same
    short envelope unless `IdentityClient.failures` is read. Without this, three
    incidents share one symptom: `below_expected_floor` and nothing else."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock, status=503)

    envelopes = await capture(lake)

    reasons = {e["reason"] for e in envelopes[CONTRACT_STATUS].errors}
    assert "identity_upstream_error" in reasons, reasons


@respx.mock
async def test_an_unmapped_position_does_not_422_the_whole_batch(lake):
    """Hotel's OverTheCap position is `ED`, which `player-identity` does not
    know — and `/resolve/batch` validates the WHOLE body, so passing it through
    would fail all 500 queries in its chunk and read as an outage.

    The fixture mock reproduces that 422 deliberately. This test is what proves
    the mapping is doing something: with it, everybody resolves; without it,
    nobody does.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    published = {row["player_id"] for row in envelopes[CONTRACT_STATUS].signals}
    assert CANONICAL_IDS["Hotel Rusher"] in published
    assert CANONICAL_IDS["Alpha Passer"] in published
    reasons = {e["reason"] for e in envelopes[CONTRACT_STATUS].errors}
    assert "identity_upstream_error" not in reasons, reasons


@respx.mock
async def test_a_raw_position_code_would_have_failed_the_batch(lake):
    """The other arm — proof the test above is not vacuous.

    An adapter that passed `ED` straight through loses EVERY row in the chunk,
    not just Hotel's. Simulated by removing the mapping, which is the smallest
    faithful stand-in for the bug.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    original = dict(upstream_mod.OTC_POSITIONS)
    upstream_mod.OTC_POSITIONS["ED"] = "ED"
    try:
        envelopes = await capture(lake)
    finally:
        upstream_mod.OTC_POSITIONS.clear()
        upstream_mod.OTC_POSITIONS.update(original)

    assert envelopes[CONTRACT_STATUS].signals == []
    reasons = {e["reason"] for e in envelopes[CONTRACT_STATUS].errors}
    assert "identity_upstream_error" in reasons, reasons


@respx.mock
async def test_a_multi_club_row_still_resolves_and_publishes_a_null_team(lake):
    """Echo's upstream `team` is `DEN/SEA`, whose ordering is not reliable. The
    row is not dropped — the contract terms are still real — and no club is
    guessed at either end: `None` is sent to `player-identity` and `null` is
    published."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    echo = next(
        row
        for row in envelopes[CONTRACT_STATUS].signals
        if row["player_id"] == CANONICAL_IDS["Echo Kicker"]
    )
    assert echo["team"] is None
    assert echo["null_field_reasons"]["team"] == "absent_in_upstream_row"


@respx.mock
async def test_a_team_defense_is_not_owed_a_contract_record(lake):
    """`roster-scope` mints one per club and they cannot sign anything.
    Expecting them would peg the coverage ratio 32 slots short forever."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert not any(
        key.startswith(f"player:{TEAM_DEFENSE_PREFIX}")
        for key in envelopes[CONTRACT_STATUS].coverage.missing
    )


def test_individual_players_excludes_team_defenses():
    scope = scope_envelope()
    members = frozenset(row["player_id"] for row in scope.signals)
    resolved = individual_players(
        Scope(
            members=members,
            captured_at=NOW,
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
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert envelopes[CONTRACT_STATUS].signals


@respx.mock
async def test_no_name_no_query_the_row_never_reaches_player_identity(lake):
    """`player-identity`'s `build_query` 422s a query with nothing to match on,
    and this feed's only match attribute is the name. The adapter drops a
    nameless row before it can become one — otherwise one blank cell would fail
    its whole chunk, exactly like an unmapped position."""
    rows = [
        row("", otc_id=1),
        row("Alpha Passer", otc_id=2, team="Packers", year_signed=2025,
            years=5, gsis_id="00-0000001"),
    ]  # fmt: skip
    mock_upstream(respx.mock, body=contracts_parquet(rows))
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert [r["player_id"] for r in envelopes[CONTRACT_STATUS].signals] == [
        CANONICAL_IDS["Alpha Passer"]
    ]
    reasons = {e["reason"] for e in envelopes[CONTRACT_STATUS].errors}
    assert "upstream_rows_malformed" in reasons, reasons


@respx.mock
async def test_the_two_query_arms_are_sent_as_the_crosswalk_expects(lake):
    """What actually goes on the wire, per arm.

    A `gsis_id` earns a **Tier-1 crosswalk adoption**: `source`/`source_id` and
    deliberately **no name**, so a crosswalk miss cannot fall through to
    attribute scoring and quietly link two players who share one. Without a
    `gsis_id` there is no crosswalk route at all, so the name is sent and the
    row lands in Tier-3 weighted agreement.

    `otc_id` is never sent under either arm: `otc` is not in
    `player_identity.identity.CROSSWALK_SOURCES`, so it could match at neither
    Tier 1 nor Tier 2 — it would be inert traffic dressed as a join, and the
    service would not even complain.
    """
    mock_upstream(respx.mock)
    route = respx.mock.post(f"{IDENTITY_URL}/resolve/batch")
    mock_identity(respx.mock)

    await capture(lake)

    assert route.call_count == 1
    queries = json.loads(route.calls[0].request.content)["queries"]
    assert queries, "no queries were sent"

    crosswalk = [q for q in queries if q.get("source")]
    by_name = [q for q in queries if not q.get("source")]
    assert crosswalk, "no query took the Tier-1 arm; the gsis split is not wired"
    assert by_name, "no query took the Tier-3 arm; the fallback is not exercised"

    for q in crosswalk:
        assert q["source"] == "gsis"
        assert q["source_id"].startswith("00-")
        assert not q.get("name"), (
            "a crosswalk query carried a name — a crosswalk miss would fall "
            "through to attribute scoring"
        )
    for q in by_name:
        assert q["name"]
        assert q.get("source_id") is None

    # No arm ever offers the OverTheCap key as an identity.
    assert all(q.get("source") != "otc" for q in queries)
    assert all(str(q.get("source_id") or "").isdigit() is False for q in queries)

    # The canonicalisers ran on the way out, not the raw upstream strings.
    assert {q["team"] for q in queries} >= {"GB", "BUF", "SF"}
    assert None in {q["team"] for q in queries}  # Echo's DEN/SEA
    assert "DE" in {q["position"] for q in queries}  # Hotel's ED


@respx.mock
async def test_a_row_with_no_gsis_id_still_resolves_by_name(lake):
    """The Tier-3 arm end to end. 23% of live active rows have no crosswalk key,
    and a collector that only handled the Tier-1 arm would silently drop every
    one of them — Delta, Echo and Hotel here."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    published = {r["player_id"] for r in envelopes[CONTRACT_STATUS].signals}
    for name in ("Delta Blocker", "Echo Kicker", "Hotel Rusher"):
        assert CANONICAL_IDS[name] in published, name


@respx.mock
async def test_a_row_WITH_a_gsis_id_resolves_through_the_crosswalk_not_the_name(lake):
    """The other arm, isolated so it cannot pass by falling back.

    The identity mock resolves a crosswalk query **only** through `GSIS_TO_NAME`.
    Here the display name is one the mock has never heard of, so the row can
    only resolve if the `gsis_id` was actually sent and adopted — which is what
    makes this a test of the Tier-1 path rather than of the name path.
    """
    mock_upstream(
        respx.mock,
        body=contracts_parquet(
            [
                row(
                    "Totally Different Name",
                    otc_id=1,
                    team="Packers",
                    year_signed=2025,
                    years=5,
                    gsis_id="00-0000001",
                )
            ]
        ),
    )
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    published = {r["player_id"] for r in envelopes[CONTRACT_STATUS].signals}
    assert published == {CANONICAL_IDS["Alpha Passer"]}


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

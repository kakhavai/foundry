"""Coverage, which is the thing most likely to be got wrong — in BOTH halves.

`coverage.expected` must never derive from what a fetch returned. That is the
half everybody remembers, and the first section below is it.

The second half is the one that gets missed, and on this collector getting it
backwards is catastrophic. The spec: "a record with zero injury events and
`sample_size_events = 0` is present and complete, not missing — a clean history
is data." A `present` predicate that required an injury event would mark **every
healthy player in the league** as a coverage hole, peg `collector_coverage_ratio`
near zero forever, and destroy the one gauge that can see a real narrowing
failure. So the `present` side is mutated here as deliberately as the `expected`
side.
"""

import httpx
import respx

from durability_history.capture import (
    DURABILITY_PROFILE,
    EXPECTED_FLOOR,
    INJURY_HISTORY,
    RETURN_TRAJECTORY,
    SIGNAL_TYPES,
    capture_durability_history,
    player_key,
)

from .conftest import (
    ALPHA,
    BRAVO,
    CANONICAL_IDS,
    CHARLIE,
    DELTA,
    ECHO,
    NOW,
    SCOPED_IDS,
    SEASON,
    WEEK,
    SpyLake,
    mock_identity,
    mock_upstreams,
    players_csv,
    scope_envelope,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


# ── the floor: `expected` never derives from the fetch ───────────────────────


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())


def test_the_floor_is_the_league_config_not_a_fetched_count():
    """32 teams x 12 individual scope slots. A constant, decided before any
    fetch: an expectation taken from the scope would make a truncated scope read
    as a perfect pass."""
    assert all(floor == 384 for floor in EXPECTED_FLOOR.values())


@respx.mock
async def test_a_five_player_scope_does_not_report_full_coverage(lake):
    """The failure the floor exists for. Five resolved rows must not yield
    `expected: 5, present: 5`, ratio 1.0."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.present == len(SCOPED_IDS)
        assert envelope.coverage.ratio < 1.0
        reasons = {error["reason"] for error in envelope.errors}
        assert "below_expected_floor" in reasons, reasons


@respx.mock
async def test_a_truncated_scope_does_not_read_as_a_perfect_pass():
    """`Coverage.ratio` returns 1.0 when `expected` is 0, so a scope naming two
    players would read as a perfect week without the floor."""
    lake = SpyLake()
    lake.write(scope_envelope(player_ids=SCOPED_IDS[:2], include_team_defenses=False))
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.present == 2
        assert envelope.coverage.ratio < 0.01


@respx.mock
async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one."""
    extra = [f"fdy-extra{index:08d}" for index in range(500)]
    lake = SpyLake()
    lake.write(
        scope_envelope(player_ids=SCOPED_IDS + extra, include_team_defenses=False)
    )
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        assert envelope.coverage.expected == len(SCOPED_IDS) + len(extra)
        assert envelope.coverage.expected > EXPECTED_FLOOR[envelope.signal_type]


# ── the `present` predicate: a clean history is DATA ─────────────────────────


@respx.mock
async def test_a_player_with_zero_injury_events_is_PRESENT_not_missing(lake):
    """The spec, verbatim: "a record with zero injury events and
    `sample_size_events = 0` is present and complete, not missing".

    Alpha and Echo have never been designated for anything. Failing them would
    mark every healthy player in the league as a coverage hole.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for signal_type in SIGNAL_TYPES:
        missing = envelopes[signal_type].coverage.missing
        for gsis in (ALPHA, ECHO):
            assert player_key(CANONICAL_IDS[gsis]) not in missing, (
                f"{signal_type}: a player with a clean history was marked missing"
            )

    history = envelopes[INJURY_HISTORY].signals
    clean = next(r for r in history if r["player_id"] == CANONICAL_IDS[ALPHA])
    assert clean["injury_events"] == []
    assert clean["sample_size_events"] == 0


@respx.mock
async def test_a_player_whose_absences_are_all_non_injury_is_PRESENT(lake):
    """Charlie missed four of six games and none of them was an injury. He is a
    complete record, not a hole — and not a durability problem either."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for signal_type in SIGNAL_TYPES:
        assert (
            player_key(CANONICAL_IDS[CHARLIE])
            not in envelopes[signal_type].coverage.missing
        )


@respx.mock
async def test_a_scope_slot_no_upstream_row_covered_IS_missing():
    """The other direction. A watchlist player the feed never mentions is a
    hole against the watchlist — not a row that quietly disappeared from both
    numerator and denominator, which reads as perfect coverage."""
    ghost = "fdy-ghost0000001"
    lake = SpyLake()
    lake.write(
        scope_envelope(player_ids=[*SCOPED_IDS, ghost], include_team_defenses=False)
    )
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for signal_type in SIGNAL_TYPES:
        envelope = envelopes[signal_type]
        assert player_key(ghost) in envelope.coverage.missing
        assert envelope.coverage.present == len(SCOPED_IDS)
        reasons = {error["reason"] for error in envelope.errors}
        assert "player_not_in_upstream" in reasons, reasons


@respx.mock
async def test_an_unresolved_player_is_missing_never_adopted_under_a_raw_id(lake):
    """`player-identity` refusing is an answer, not a gap to paper over. A
    defaulting `.get(query, row.gsis_id)` would publish the feed's own GSIS key
    as a player id and score the slot present."""
    mock_upstreams(respx.mock)
    resolvable = {g: p for g, p in CANONICAL_IDS.items() if g != BRAVO}
    mock_identity(respx.mock, resolvable=resolvable)

    envelopes = await capture(lake)

    for signal_type in SIGNAL_TYPES:
        envelope = envelopes[signal_type]
        assert player_key(CANONICAL_IDS[BRAVO]) in envelope.coverage.missing
        published = {row["player_id"] for row in envelope.signals}
        assert BRAVO not in published, "a raw GSIS id was adopted as a player_id"
        assert CANONICAL_IDS[BRAVO] not in published


@respx.mock
async def test_an_unenumerable_tenure_is_a_PROFILE_hole_only():
    """A slot with no observed game has no denominator, so
    `availability_rate` is null and the profile answers nothing — that IS a hole.

    The injury history and the trajectory are still complete answers for him
    ("we looked; there is nothing"), so scoring all three the same way would
    either hide a real profile gap or invent two fake ones.
    """
    # Delta has no pfr_id and exactly one designation; strip that designation and
    # nothing anywhere observes him.
    lake = SpyLake()
    lake.write(scope_envelope())
    mock_upstreams(respx.mock, injuries=dict.fromkeys([2024, 2025, 2026], ""))
    mock_identity(respx.mock)
    # An empty injuries document is a schema error, so give a header-only one.
    from durability_history.adapters import upstream as upstream_mod

    header = (
        "season,game_type,team,week,gsis_id,position,full_name,first_name,"
        "last_name,report_primary_injury,report_secondary_injury,report_status,"
        "practice_primary_injury,practice_secondary_injury,practice_status,"
        "date_modified\n"
    )
    for season in (2024, 2025, 2026):
        respx.mock.get(upstream_mod.INJURIES_URL.format(season=season)).respond(
            200, text=header
        )

    envelopes = await capture(lake)

    key = player_key(CANONICAL_IDS[DELTA])
    assert key in envelopes[DURABILITY_PROFILE].coverage.missing
    assert key not in envelopes[INJURY_HISTORY].coverage.missing
    assert key not in envelopes[RETURN_TRAJECTORY].coverage.missing
    reasons = {e["reason"] for e in envelopes[DURABILITY_PROFILE].errors}
    assert "tenure_not_enumerable" in reasons, reasons


@respx.mock
async def test_a_dropped_out_of_scope_row_is_not_a_coverage_miss(lake):
    """An out-of-scope player was never owed, so recording one would make
    narrowing read as a permanent coverage regression that buries the rows that
    genuinely failed."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        assert not any("fdy-ffff" in key for key in envelope.coverage.missing)


@respx.mock
async def test_a_position_this_collector_does_not_carry_is_not_expected(lake):
    """`players_csv` carries a defensive tackle and a retired running back. The
    expectation is the SCOPE, so neither can move `expected` in either
    direction."""
    mock_upstreams(respx.mock, players=players_csv())
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        assert envelope.coverage.expected == 384

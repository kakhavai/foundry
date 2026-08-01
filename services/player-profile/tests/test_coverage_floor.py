"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned, and on a
scope-aware collector it must not derive from the scope's size either: the scope
is itself a fetch, and `Coverage.ratio` returns 1.0 when `expected` is 0.

These are the tests that fail if somebody "simplifies" `capture.py` by computing
the expectation from the rows it resolved or from `len(scope.members)`.
"""

from datetime import timedelta

import httpx
import respx

from player_profile.capture import (
    ATHLETICISM,
    BIOGRAPHICAL,
    EXPECTED_FLOOR,
    LEAGUE_TEAMS,
    SIGNAL_TYPES,
    capture_player_profile,
    player_key,
)

from .conftest import (
    CANONICAL_IDS,
    FIXTURE_PLAYERS,
    NOW,
    SCOPED_IDS,
    SEASON,
    TEAM_DEFENSE_IDS,
    WEEK,
    SpyLake,
    mock_identity,
    mock_upstreams,
    players_csv,
    scope_envelope,
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_profile(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def test_every_signal_type_declares_the_same_league_derived_floor():
    """384 = 32 teams x 12 individual scope slots. A fact about roster-scope's
    config, decided before any fetch."""
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert set(EXPECTED_FLOOR.values()) == {384}
    assert EXPECTED_FLOOR[BIOGRAPHICAL] == LEAGUE_TEAMS * 12


@respx.mock
async def test_a_truncated_scope_does_not_report_full_coverage(lake):
    """The failure the floor exists for, in its scope-aware form.

    A scope naming five players that all resolve must not yield
    `expected: 5, present: 5`, ratio 1.0 — that reads as a flawless week while
    379 of the league's watchlist slots are simply absent.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type], signal_type
        assert envelope.coverage.present == len(SCOPED_IDS), signal_type
        assert envelope.coverage.ratio < 1.0, signal_type
        reasons = {error["reason"] for error in envelope.errors}
        assert "below_expected_floor" in reasons, reasons


@respx.mock
async def test_a_scope_slot_no_upstream_row_covered_is_a_recorded_miss(lake):
    """Narrowing must not shrink numerator and denominator together.

    A watchlist player `players.csv` has no row for is a HOLE. Dropping them
    from both sides would read as perfect coverage of a smaller league.
    """
    mock_upstreams(respx.mock)
    # Alpha is refused by player-identity, so no upstream row can cover his slot.
    resolvable = {
        gsis: pid for gsis, pid in CANONICAL_IDS.items() if gsis != "00-0000001"
    }
    mock_identity(respx.mock, resolvable=resolvable)

    envelopes = await capture(lake)

    missing = envelopes[BIOGRAPHICAL].coverage.missing
    assert player_key(CANONICAL_IDS["00-0000001"]) in missing
    assert envelopes[BIOGRAPHICAL].coverage.present == len(SCOPED_IDS) - 1
    reasons = {error["reason"] for error in envelopes[BIOGRAPHICAL].errors}
    assert "profile_not_in_upstream" in reasons, reasons


@respx.mock
async def test_team_defenses_are_not_owed_a_profile():
    """`roster-scope`'s 416 slots include 32 team defenses, which have no birth
    date, no draft pick and no combine result.

    Counting them as owed would peg coverage at 384/416 forever and file 32
    identical errors on every single pass — a permanent red that trains an
    operator to ignore the signal.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    lake = SpyLake()
    lake.write(scope_envelope())

    envelopes = await capture(lake)

    missing = envelopes[BIOGRAPHICAL].coverage.missing
    assert not any(player_key(dst) in missing for dst in TEAM_DEFENSE_IDS)
    published = {row["player_id"] for row in envelopes[BIOGRAPHICAL].signals}
    assert published.isdisjoint(TEAM_DEFENSE_IDS)
    assert len(published) == len(SCOPED_IDS)


@respx.mock
async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    # 400 extra watchlist ids nothing upstream covers, pushing the observed
    # universe past 384.
    extras = [f"fdy-extra{index:06d}" for index in range(400)]
    lake = SpyLake()
    lake.write(scope_envelope(player_ids=[*SCOPED_IDS, *extras]))

    envelopes = await capture(lake)

    for envelope in envelopes.values():
        assert envelope.coverage.expected == len(SCOPED_IDS) + len(extras)
        assert envelope.coverage.present == len(SCOPED_IDS)


@respx.mock
async def test_athleticism_coverage_does_not_punish_an_untested_player(lake):
    """The spec: "athletic measurements are explicitly optional and their
    absence does not count as missing."

    Two of the fixture players have no combine row at all. Scoring them as
    misses would peg the ratio near 0.56 forever — measured across the real
    feed, only 749 of 1,326 recent players have one.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    athleticism = envelopes[ATHLETICISM]
    untested = [
        row for row in athleticism.signals if row["athleticism"]["forty_yard"] is None
    ]
    assert untested, "the fixture no longer has an untested player"
    assert athleticism.coverage.present == len(SCOPED_IDS)
    for row in untested:
        assert player_key(row["player_id"]) not in athleticism.coverage.missing


@respx.mock
async def test_biographical_needs_experience_seasons_where_athleticism_does_not(
    lake,
):
    """**The spec's per-signal-type asymmetry**, which is the whole reason
    `capture.py` has an if/else there rather than one `record` call:

    > every player in the current roster-scope watchlist has a profile record
    > with a non-null `player_id`, `position`, and `experience_seasons`;
    > athletic measurements are explicitly optional and their absence does not
    > count as missing.

    So one player with a blank `years_of_experience` must be **missing from
    `player_biographical` and present in `player_athleticism` at the same
    time**. Both halves are asserted, because either alone is satisfied by a
    collector that scores every signal type identically — which is exactly the
    mutation that used to survive here: replacing the whole predicate with an
    unconditional `record` left all 120 tests green.

    The row is still PUBLISHED on both. A coverage hole is not a dropped row —
    dropping it would shrink numerator and denominator together and read as a
    perfect pass over a smaller league.
    """
    short = [list(record) for record in FIXTURE_PLAYERS]
    short[2][9] = ""  # Charlie: blank years_of_experience
    mock_upstreams(respx.mock, players=players_csv(short))
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    key = player_key(CANONICAL_IDS["00-0000003"])
    biographical = envelopes[BIOGRAPHICAL]
    athleticism = envelopes[ATHLETICISM]

    # Half one: incomplete for biography.
    assert key in biographical.coverage.missing
    assert biographical.coverage.present == len(SCOPED_IDS) - 1
    reasons = {error["reason"] for error in biographical.errors}
    assert "profile_incomplete" in reasons, reasons

    # Half two: the SAME player, the same pass, complete for athleticism.
    assert key not in athleticism.coverage.missing
    assert athleticism.coverage.present == len(SCOPED_IDS)

    # And published on both, carrying the null that made it incomplete.
    charlie = next(
        row
        for row in biographical.signals
        if row["player_id"] == CANONICAL_IDS["00-0000003"]
    )
    assert charlie["experience_seasons"] is None


@respx.mock
async def test_a_deadline_records_the_remainder_as_missing_rather_than_dropping_it(
    lake, monkeypatch
):
    """The truncation path: "a truncated pass that reports itself truncated is
    useful; one that reports itself complete is not."

    The clock crosses the deadline **partway through** the loop rather than
    before it, because a fully-truncated pass cannot tell "recorded the rest as
    missing" apart from "captured nothing" — the interesting claim is that the
    rows which already resolved are kept while the rest become explicit misses.
    """

    class CrossingClock:
        """`datetime.now` that steps past `deadline` after `cross_after` calls."""

        def __init__(self, deadline, cross_after):
            self.deadline = deadline
            self.cross_after = cross_after
            self.calls = 0

        def now(self, tz=None):
            self.calls += 1
            offset = 1 if self.calls > self.cross_after else -1
            return self.deadline + timedelta(seconds=offset)

    deadline = NOW + timedelta(seconds=30)
    monkeypatch.setattr(
        "player_profile.capture.datetime", CrossingClock(deadline, cross_after=2)
    )
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake, deadline=deadline)

    biographical = envelopes[BIOGRAPHICAL]
    # Two rows beat the clock; the other three are recorded, not discarded.
    assert biographical.coverage.present == 2
    assert len(biographical.signals) == 2
    assert len(biographical.coverage.missing) == len(SCOPED_IDS) - 2
    reasons = {error["reason"] for error in biographical.errors}
    assert "deadline_exceeded" in reasons, reasons
    # Every signal type truncates together — a pass that kept a full athleticism
    # envelope beside a truncated biographical one would be internally
    # inconsistent about which players it saw.
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.present == 2, signal_type


@respx.mock
async def test_a_player_identity_outage_is_named_rather_than_a_quiet_short_week(
    lake,
):
    """`resolve_many` returns a partial dict when a chunk's REQUEST fails and
    records the reason on `.failures`. A caller reading only the dict reports a
    total outage as an ordinary short week whose errors say nothing but
    `below_expected_floor`."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock, status=503)

    envelopes = await capture(lake)

    reasons = {error["reason"] for error in envelopes[BIOGRAPHICAL].errors}
    assert "identity_upstream_error" in reasons, reasons
    # Summarised, never per row: an uncapped list would push every other reason
    # off `CoverageAccumulator`'s 50-entry cap.
    outage_entries = [
        error
        for error in envelopes[BIOGRAPHICAL].errors
        if error["reason"] == "identity_upstream_error"
    ]
    assert len(outage_entries) == 1

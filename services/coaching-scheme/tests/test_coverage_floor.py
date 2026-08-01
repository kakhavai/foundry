"""`coverage.expected` never derives from what succeeded — both halves.

**Both halves, on each signal type independently.** A set attacking only
`expected` scored 34/34 on an earlier collector and still missed a deleted
`present` check, so every claim below comes in pairs: what the universe is,
and what makes a key count as present in it.

The two signal types are keyed differently on purpose and that is itself a
load-bearing claim, tested here:

* `staff_assignment` is keyed by **team**, per the spec's coverage sentence.
* `team_scheme_profile` is keyed by **revision**, because a revision is what
  owes a profile — keying it by team would let one revision of three stand in
  for the other two.
"""

import pytest
from collector_core.coverage import BELOW_EXPECTED_FLOOR

from coaching_scheme.capture import (
    EXPECTED_FLOOR,
    PROFILE,
    REASON_NO_GAMES_SAMPLED,
    REASON_NO_REVISION,
    REASON_PLAY_CALLER_REGISTER_EMPTY,
    STAFF,
)
from coaching_scheme.play_callers import REASON_UNKNOWN, PlayCallerAssertion

from .conftest import (
    LATER,
    TEAMS,
    Feeds,
    SpyLake,
    coaches_with_change,
    run_capture,
    steady_coaches,
)

SIGNAL_TYPES = (STAFF, PROFILE)


# --------------------------------------------------------------------------
# Half one: `expected` is declared, never derived
# --------------------------------------------------------------------------


@pytest.mark.parametrize("signal_type", SIGNAL_TYPES)
def test_the_floor_is_thirty_two_for_both_signal_types(signal_type):
    """32 teams since the 2002 realignment, and every team has at least one
    revision — so 32 is the floor for both universes, from two different
    facts."""
    assert EXPECTED_FLOOR[signal_type] == 32


@pytest.mark.parametrize("signal_type", SIGNAL_TYPES)
async def test_a_four_team_feed_reports_thirty_two_expected(signal_type, lake: SpyLake):
    """**The core claim.** The fixture describes four teams. A collector that
    derived its expectation from the document would report `expected: 4,
    present: 4`, ratio 1.0 — perfectly healthy, with 88% of the league gone."""
    envelopes = await run_capture(lake=lake)
    coverage = envelopes[signal_type].coverage
    assert coverage.expected == 32
    assert coverage.present <= len(TEAMS)
    assert coverage.ratio < 1.0


@pytest.mark.parametrize("signal_type", SIGNAL_TYPES)
async def test_the_shortfall_is_stated_in_errors_not_left_to_be_inferred(
    signal_type, lake: SpyLake
):
    """`missing` is short while `expected` is not, and that gap is real
    information. It is also stated outright rather than left to a reader
    subtracting two numbers."""
    envelopes = await run_capture(lake=lake)
    reasons = [error["reason"] for error in envelopes[signal_type].errors]
    assert BELOW_EXPECTED_FLOOR in reasons


async def test_a_truncated_upstream_lowers_the_ratio_rather_than_the_universe(
    lake: SpyLake,
):
    """One team instead of four. `expected` must not follow the document
    down — that is the whole failure the floor defeats."""
    one_team = {"AAA": steady_coaches(("AAA",))["AAA"]}
    envelopes = await run_capture(Feeds(coaches=one_team), lake=lake)
    for signal_type in SIGNAL_TYPES:
        assert envelopes[signal_type].coverage.expected == 32


async def test_a_richer_season_raises_expected_above_the_floor(lake: SpyLake):
    """A floor never CAPS a genuine count.

    `team_scheme_profile` is keyed by revision, so a mid-season coaching
    change creates a 33rd... except the fixture has four teams, not 32. So
    prove the mechanism instead: five revisions across four teams is still
    floored to 32, and the observed count is what moves. Pinned via the
    accumulator's own arithmetic rather than by needing a 32-team fixture.
    """
    from collector_core.coverage import CoverageAccumulator

    acc = CoverageAccumulator(floor=32)
    for index in range(40):
        acc.expect(f"r{index}")
        acc.record(f"r{index}")
    assert acc.result().expected == 40


# --------------------------------------------------------------------------
# Half two: what makes a key PRESENT
# --------------------------------------------------------------------------


async def test_staff_present_measures_the_grid_clause_not_the_play_caller(
    lake: SpyLake,
):
    """**DEVIATION 5, and the reason for it.**

    The spec's coverage sentence has two clauses — a revision covering the
    week, AND a non-null play-caller. Scoring both makes the second swallow
    the first: with the register empty, `present` would be 0 whether
    `games.csv` carried 32 teams or 3, so the ratio could never report a
    truncated schedule feed. The clause that IS sourceable becomes
    unobservable behind the clause that is not.

    So `present` counts teams with a covering revision — true, checkable, and
    otherwise unmeasured — and play-caller completeness moves to its own
    gauge and the per-row reason. Every team here has a revision covering
    week 1, so all four are present despite an empty register.
    """
    envelopes = await run_capture(lake=lake)
    coverage = envelopes[STAFF].coverage
    assert coverage.present == len(TEAMS)
    assert not set(coverage.missing) & set(TEAMS)
    # The play-caller gap is not lost — it moves, losslessly, to three places.
    assert REASON_PLAY_CALLER_REGISTER_EMPTY in {
        error["reason"] for error in envelopes[STAFF].errors
    }
    assert all(
        row["play_caller_missing_reason"] == REASON_UNKNOWN
        for row in envelopes[STAFF].signals
    )


async def test_a_truncated_grid_now_lowers_the_staff_ratio(lake: SpyLake):
    """**The failure deviation 5 exists to restore.**

    One team instead of four. Under the literal two-clause predicate this was
    unobservable — `present` was 0 either way — so the suite could only ever
    assert `expected == 32` and never that the ratio moved. Now it moves.
    """
    full = await run_capture(lake=lake)
    one_team = {"AAA": steady_coaches(("AAA",))["AAA"]}
    truncated = await run_capture(Feeds(coaches=one_team), lake=SpyLake(), now=LATER)
    assert full[STAFF].coverage.present == len(TEAMS)
    assert truncated[STAFF].coverage.present == 1
    assert truncated[STAFF].coverage.ratio < full[STAFF].coverage.ratio


async def test_the_play_caller_gauge_moves_independently_of_coverage(
    lake: SpyLake, monkeypatch
):
    """The other half of deviation 5: the unsourced field keeps its own dial.

    Same grid, same coverage, one curated entry — the gauge moves and the
    ratio does not. Without this, play-caller completeness could have been
    dropped rather than relocated, and the deviation would be a quiet loss of
    information rather than a move.
    """
    from coaching_scheme import play_callers

    before = await run_capture(lake=lake)
    assert before[STAFF].coverage.present == len(TEAMS)

    monkeypatch.setattr(
        play_callers,
        "ASSERTIONS",
        (
            PlayCallerAssertion(
                team_id="AAA",
                season=2026,
                play_caller_id="coach-someone",
                play_caller_role="offensive_coordinator",
                effective_from_week=1,
                # The whole revision span, or `resolve_for_span` refuses it.
                asserted_through_week=12,
                source="https://example.test/report",
            ),
        ),
    )
    after = await run_capture(lake=SpyLake(), now=LATER)
    assert after[STAFF].coverage.present == len(TEAMS)
    resolved = [
        row for row in after[STAFF].signals if row["play_caller_id"] is not None
    ]
    assert {row["team_id"] for row in resolved} == {"AAA"}


async def test_a_team_with_no_revision_covering_the_week_is_missing(lake: SpyLake):
    """The spec's first clause.

    AAA does not appear in the grid until week 4, so at week 2 it has no
    revision covering the capture — `effective_to_week is None` means
    *current*, which must not be read as *always*. A `covers()` that only
    checked the upper bound would say every open revision covers week 1.
    """
    partial = steady_coaches(weeks=6)
    partial["AAA"] = {week: "Coach AAA" for week in range(4, 7)}
    envelopes = await run_capture(Feeds(coaches=partial), lake=lake, week=2)
    errors = {error["reason"] for error in envelopes[STAFF].errors}
    assert REASON_NO_REVISION in errors
    assert "AAA" in envelopes[STAFF].coverage.missing
    # The other three DO have a covering revision, so they are present — the
    # grid clause is what coverage now measures, and the two failure modes
    # stay distinguishable.
    assert envelopes[STAFF].coverage.present == len(TEAMS) - 1


async def test_profile_present_requires_a_sampled_game(lake: SpyLake):
    """A revision that has not played is expected and missing, not
    present-with-nulls. Its row still publishes — 'this regime exists and has
    no games' is a fact worth serving.

    AAA's coach changes at week 12; play-by-play only reaches week 11. So the
    new regime is real, scheduled, and has no snaps yet — the state every
    interim starts in.
    """
    from .conftest import flat_proe

    envelopes = await run_capture(
        Feeds(
            coaches=coaches_with_change("AAA", at_week=12, weeks=12),
            proe=flat_proe(weeks=11),
        ),
        lake=lake,
    )
    profile = envelopes[PROFILE]
    unplayed = [row for row in profile.signals if row["games_sampled"] == 0]
    assert unplayed, "no unplayed revision in the fixture"
    for row in unplayed:
        assert row["revision_id"] in profile.coverage.missing
        # Published, not dropped.
        assert row["sampled_weeks"] == []
    assert REASON_NO_GAMES_SAMPLED in {error["reason"] for error in profile.errors}


async def test_profile_present_for_a_revision_that_did_play(lake: SpyLake):
    """The other side. Without it, `record` could be deleted entirely and
    every test above would still pass."""
    envelopes = await run_capture(lake=lake)
    profile = envelopes[PROFILE]
    assert profile.coverage.present == len(TEAMS)
    assert not set(profile.coverage.missing) & set(TEAMS)


# --------------------------------------------------------------------------
# The keying difference between the two signal types
# --------------------------------------------------------------------------


async def test_staff_coverage_counts_teams_and_profile_counts_revisions(
    lake: SpyLake,
):
    """A mid-season change adds a revision but not a team.

    So `staff_assignment`'s observed universe stays at four while
    `team_scheme_profile`'s becomes five. Collapse the two keyings into one
    and exactly one of those two numbers goes wrong — and which one depends
    on which keying survived, so this pins both.
    """
    envelopes = await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7)), lake=lake
    )
    staff_keys = set(envelopes[STAFF].coverage.missing) | {
        row["team_id"] for row in envelopes[STAFF].signals
    }
    assert staff_keys == set(TEAMS)

    profile_rows = envelopes[PROFILE].signals
    assert len({row["revision_id"] for row in profile_rows}) == len(TEAMS) + 1
    assert envelopes[PROFILE].coverage.present == len(TEAMS) + 1


async def test_a_failed_pass_still_writes_a_present_zero_envelope(lake: SpyLake):
    """`fail_capture` on the fatal feed: one `present: 0` envelope per signal
    type with a populated `errors` array, then a re-raise so `CaptureState`
    does not install an empty capture over the last good one."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(games_status=500), lake=lake)

    assert {envelope.signal_type for envelope in lake.writes} == set(SIGNAL_TYPES)
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == 32
        assert envelope.errors


# --------------------------------------------------------------------------
# F7 — the priority error is the only entry that says where the fix goes
# --------------------------------------------------------------------------


async def test_the_priority_error_counts_only_teams_it_can_speak_for(
    lake: SpyLake,
):
    """A team with no covering revision is missing for a different reason.

    Counting it in "N teams have no in-force play-caller assertion" sends an
    operator to curate `play_callers.py` for a team whose problem is that the
    schedule feed does not carry it. This is the only error that names a
    remedy, so a wrong attribution in it costs more than its size suggests.

    AAA appears only from week 4, so at week 2 three of four teams are
    covered — and the message must say three, not four.
    """
    partial = steady_coaches(weeks=6)
    partial["AAA"] = {week: "Coach AAA" for week in range(4, 7)}
    envelopes = await run_capture(Feeds(coaches=partial), lake=lake, week=2)

    priority = [
        error
        for error in envelopes[STAFF].errors
        if error["reason"] == REASON_PLAY_CALLER_REGISTER_EMPTY
    ]
    assert len(priority) == 1
    detail = priority[0]["detail"]
    assert "3 of 3 team(s)" in detail, detail
    assert "week 2" in detail
    assert "play_callers.py" in detail


async def test_no_priority_error_when_every_covered_team_is_curated(
    lake: SpyLake, monkeypatch
):
    """The negative control. Without it the error could be unconditional and
    the attribution test above would still pass."""
    from coaching_scheme import play_callers

    monkeypatch.setattr(
        play_callers,
        "ASSERTIONS",
        tuple(
            PlayCallerAssertion(
                team_id=team,
                season=2026,
                play_caller_id=f"coach-{team.lower()}",
                play_caller_role="unknown",
                effective_from_week=1,
                asserted_through_week=12,
                source="https://example.test/report",
            )
            for team in TEAMS
        ),
    )
    envelopes = await run_capture(lake=lake)
    assert REASON_PLAY_CALLER_REGISTER_EMPTY not in {
        error["reason"] for error in envelopes[STAFF].errors
    }
    assert all(row["play_caller_id"] is not None for row in envelopes[STAFF].signals)


def _register(teams, *, through=12, weeks_from=1):
    return tuple(
        PlayCallerAssertion(
            team_id=team,
            season=2026,
            play_caller_id=f"coach-{team.lower()}",
            play_caller_role="unknown",
            effective_from_week=weeks_from,
            asserted_through_week=through,
            source="https://example.test/report",
        )
        for team in teams
    )


async def test_an_uncovered_team_does_not_trigger_the_curation_error(
    lake: SpyLake, monkeypatch
):
    """**The condition, not just the message.**

    Every team that HAS a covering revision is curated, and one team does not
    have one. Counting against every team rather than against the covered ones
    fires a spurious "0 of 3 team(s) have no in-force assertion" — an error
    telling an operator to curate a file that is already complete.

    The message alone cannot catch this: both readings render the same string
    when nothing is curated, which is why
    `test_the_priority_error_counts_only_teams_it_can_speak_for` passed against
    the mutant.
    """
    from coaching_scheme import play_callers

    partial = steady_coaches(weeks=6)
    partial["AAA"] = {week: "Coach AAA" for week in range(4, 7)}
    covered = [team for team in TEAMS if team != "AAA"]
    monkeypatch.setattr(play_callers, "ASSERTIONS", _register(covered, through=6))

    envelopes = await run_capture(Feeds(coaches=partial), lake=lake, week=2)
    assert "AAA" in envelopes[STAFF].coverage.missing
    assert envelopes[STAFF].coverage.present == len(covered)
    assert REASON_PLAY_CALLER_REGISTER_EMPTY not in {
        error["reason"] for error in envelopes[STAFF].errors
    }

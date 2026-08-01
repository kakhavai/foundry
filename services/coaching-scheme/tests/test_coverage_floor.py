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
    STAFF,
)
from coaching_scheme.play_callers import (
    REASON_UNKNOWN,
    PlayCallerAssertion,
)

from .conftest import (
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


async def test_staff_present_requires_a_non_null_play_caller(lake: SpyLake):
    """**The spec's predicate, verbatim.** Every team here has a revision
    covering week 1 and a head coach; the register is empty, so no team is
    present. Delete the `if assertion is None` branch and this reads 4/32."""
    envelopes = await run_capture(lake=lake)
    coverage = envelopes[STAFF].coverage
    assert coverage.present == 0
    assert set(coverage.missing) == set(TEAMS)
    assert {error["reason"] for error in envelopes[STAFF].errors} >= {REASON_UNKNOWN}


async def test_staff_present_when_the_register_covers_the_team(
    lake: SpyLake, monkeypatch
):
    """The other side of the same predicate. Without this, `present` could be
    hardcoded to 0 and the test above would still pass."""
    from coaching_scheme import play_callers

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
                asserted_through_week=6,
                source="https://example.test/report",
            ),
        ),
    )
    envelopes = await run_capture(lake=lake)
    coverage = envelopes[STAFF].coverage
    assert coverage.present == 1
    assert "AAA" not in coverage.missing


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
    # And the teams that DO have a covering revision are missing for the
    # play-caller reason instead — the two clauses must stay distinguishable.
    assert REASON_UNKNOWN in errors


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

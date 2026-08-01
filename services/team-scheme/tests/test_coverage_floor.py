"""`coverage.expected` never derives from what succeeded — both halves.

**Both halves.** A set attacking only `expected` scored 34/34 on an earlier
collector and still missed a deleted `present` check, so every claim below
comes in pairs: what the universe is, and what makes a key count as present in
it.

The phase doc's sentence is *every team in the season grid has a profile with
`neutral_pass_rate` and `games_sampled` non-null; 32 teams is a declarable
floor independent of any fetch.* Both clauses of the present predicate are
scored separately, because they fail for different reasons and a combined
check makes one of them unobservable.

The team universe here comes from the **same play-by-play document that
produces the rates**, which is exactly the derivation the floor exists to
defend against. That makes the floor load-bearing rather than decorative, and
it is why the fixtures below describe four teams rather than 32.
"""

import httpx
import pytest
from collector_core.coverage import BELOW_EXPECTED_FLOOR, CoverageAccumulator

from team_scheme.capture import (
    EXPECTED_FLOOR,
    PROFILE,
    REASON_NO_GAMES_SAMPLED,
    REASON_NO_NEUTRAL_SNAPS,
)

from .conftest import (
    LATER,
    TEAMS,
    Feeds,
    SpyLake,
    flat_proe,
    run_capture,
)

# --------------------------------------------------------------------------
# Half one: `expected` is declared, never derived
# --------------------------------------------------------------------------


def test_the_floor_is_thirty_two():
    """32 teams since the 2002 realignment, and every one of them owes a
    profile. Asserted as a literal so lowering it is observable — a floor
    computed from anything is a floor that can follow the fetch down."""
    assert EXPECTED_FLOOR[PROFILE] == 32


async def test_a_four_team_document_reports_thirty_two_expected(lake: SpyLake):
    """**The core claim.** The fixture describes four teams. A collector that
    derived its expectation from the document would report `expected: 4,
    present: 4`, ratio 1.0 — perfectly healthy, with 88% of the league gone."""
    envelopes = await run_capture(lake=lake)
    coverage = envelopes[PROFILE].coverage
    assert coverage.expected == 32
    assert coverage.present == len(TEAMS)
    assert coverage.ratio < 1.0


async def test_a_truncated_document_lowers_the_ratio_rather_than_the_universe(
    lake: SpyLake,
):
    """One team instead of four. `expected` must not follow the document down
    — that is the whole failure the floor defeats — and the ratio must
    actually **move**, which is the half a floor-only assertion cannot see."""
    full = await run_capture(lake=lake)
    truncated = await run_capture(
        Feeds(proe=flat_proe(("AAA",))), lake=SpyLake(), now=LATER
    )
    assert truncated[PROFILE].coverage.expected == 32
    assert truncated[PROFILE].coverage.present == 1
    assert full[PROFILE].coverage.present == len(TEAMS)
    assert truncated[PROFILE].coverage.ratio < full[PROFILE].coverage.ratio


async def test_the_shortfall_is_stated_in_errors_not_left_to_be_inferred(
    lake: SpyLake,
):
    """`missing` is short while `expected` is not, and that gap is real
    information. It is also stated outright rather than left to a reader
    subtracting two numbers."""
    envelopes = await run_capture(lake=lake)
    reasons = [error["reason"] for error in envelopes[PROFILE].errors]
    assert BELOW_EXPECTED_FLOOR in reasons


def test_a_richer_universe_raises_expected_above_the_floor():
    """A floor never CAPS a genuine count. Pinned via the accumulator's own
    arithmetic rather than by needing a 40-team fixture, which cannot exist."""
    acc = CoverageAccumulator(floor=32)
    for index in range(40):
        acc.expect(f"T{index}")
        acc.record(f"T{index}")
    assert acc.result().expected == 40


# --------------------------------------------------------------------------
# Half two: what makes a team PRESENT
# --------------------------------------------------------------------------


async def test_a_team_that_played_and_passed_is_present(lake: SpyLake):
    """The negative control. Without it, `record` could be deleted entirely
    and every failure test below would still pass."""
    envelopes = await run_capture(lake=lake)
    coverage = envelopes[PROFILE].coverage
    assert coverage.present == len(TEAMS)
    assert not set(coverage.missing) & set(TEAMS)


async def test_a_team_with_no_games_is_expected_and_missing(lake: SpyLake):
    """Clause one of the predicate: `games_sampled` must be non-null, and a
    zero is the honest reading of a team that has not played.

    Its row still publishes — 'this team exists and has no games' is a fact
    worth serving — but it is not counted present, because a profile of
    nothing is not a profile.
    """
    proe = flat_proe(weeks=12)
    # DDD is in the document with a week whose plays are all non-offensive:
    # present in the universe, no snaps, no games.
    feeds = Feeds(proe=proe)
    for play in feeds.plays:
        if play["posteam"] == "DDD":
            play["play_type"] = "punt"
            play["punt_attempt"] = 1

    envelopes = await run_capture(feeds, lake=lake)
    profile = envelopes[PROFILE]
    unplayed = [row for row in profile.signals if row["games_sampled"] == 0]
    assert unplayed, "no unplayed team in the fixture"
    for row in unplayed:
        assert row["team_id"] in profile.coverage.missing
        assert row["sampled_weeks"] == []
    assert REASON_NO_GAMES_SAMPLED in {error["reason"] for error in profile.errors}
    assert profile.coverage.present == len(TEAMS) - len(unplayed)


async def test_a_team_that_played_only_lopsided_football_is_missing(lake: SpyLake):
    """Clause two, and it is a **separate** clause.

    CCC plays a full season with no snap in neutral script — a blowout from
    the first drive, every week. `games_sampled` is 12, so clause one is
    satisfied; `neutral_pass_rate` is null, so the headline field is not
    populated and the team is not present.

    Score the two clauses as one check and this case is unreachable: the team
    would count present on `games_sampled` alone while the field a consumer
    actually reads is null.
    """
    feeds = Feeds()
    for play in feeds.plays:
        if play["posteam"] == "CCC":
            play["qtr"] = 4
            play["wp"] = 0.02

    envelopes = await run_capture(feeds, lake=lake)
    profile = envelopes[PROFILE]
    row = next(r for r in profile.signals if r["team_id"] == "CCC")
    assert row["games_sampled"] == 12
    assert row["neutral_pass_rate"] is None
    assert "CCC" in profile.coverage.missing
    assert REASON_NO_NEUTRAL_SNAPS in {error["reason"] for error in profile.errors}
    assert profile.coverage.present == len(TEAMS) - 1


async def test_a_degraded_charting_feed_costs_no_coverage(lake: SpyLake):
    """The predicate is `neutral_pass_rate` and `games_sampled`, both of which
    come from play-by-play. So losing a charting feed nulls three fields and
    moves the ratio not at all — which is correct, and is exactly why
    `team_scheme_degraded_upstreams` exists as a separate gauge."""
    healthy = await run_capture(lake=lake)
    degraded = await run_capture(
        Feeds(ftn_status=500, participation_status=500), lake=SpyLake(), now=LATER
    )
    assert degraded[PROFILE].coverage.present == healthy[PROFILE].coverage.present
    assert degraded[PROFILE].coverage.ratio == healthy[PROFILE].coverage.ratio


async def test_a_failed_pass_still_writes_a_present_zero_envelope(lake: SpyLake):
    """`fail_capture` on the fatal feed: one `present: 0` envelope with a
    populated `errors` array, then a re-raise so `CaptureState` does not
    install an empty capture over the last good one.

    **`expected` is still 32 on that envelope.** A failure envelope that
    reported `expected: 0` would read as a collector with nothing to do.
    """
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(pbp_status=500), lake=lake)

    assert [envelope.signal_type for envelope in lake.writes] == [PROFILE]
    written = lake.writes[0]
    assert written.coverage.present == 0
    assert written.coverage.expected == 32
    assert written.errors


async def test_the_deadline_records_the_rest_as_missing_rather_than_dropping_them(
    lake: SpyLake,
):
    """Over budget. A truncated pass that reports itself truncated is useful;
    one that reports itself complete is not — and `expect` is called before
    the deadline check precisely so an abandoned team is still owed."""
    from datetime import UTC, datetime

    past = datetime(2000, 1, 1, tzinfo=UTC)
    envelopes = await run_capture(lake=lake, deadline=past)
    profile = envelopes[PROFILE]
    assert profile.coverage.expected == 32
    assert profile.coverage.present == 0
    assert set(profile.coverage.missing) == set(TEAMS)
    assert "deadline_exceeded" in {error["reason"] for error in profile.errors}

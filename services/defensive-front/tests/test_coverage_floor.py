"""The coverage predicate — **both halves of it**.

`coverage.expected` must never derive from what a fetch returned, and neither
must `present`. A mutation set that attacks only `expected` scored 34/34 on an
earlier collector and still missed a deleted `present` check, so the two are
tested as a pair here and every test says which half it defends.

This file also pins the spec deviation. The phase doc's clause is 32 defences
x three `unit` values, present only when all three are populated. With only
`overall` sourceable that predicate is 0.0 forever — and a ratio pinned at
zero cannot report a truncated upstream, a dead join or a half-empty week
either, because all of them read the same. Narrowing the floor to 32 keeps the
metric able to say something.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from defensive_front.capture import EXPECTED_FLOOR, SIGNAL_TYPES, STRENGTH
from defensive_front.ratings import UNITS

from . import season as season_module
from .conftest import Feeds, SpyLake, run_capture

TEAM_COUNT = len(season_module.TEAMS)


def test_the_floor_is_the_league_not_the_league_times_the_units():
    """The disclosed deviation, asserted rather than left in a comment: 32,
    not 96. If `UNITS` ever grows, this test is what forces a decision about
    the floor instead of letting it drift."""
    assert EXPECTED_FLOOR[STRENGTH] == 32
    assert len(UNITS) == 1


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())


# --------------------------------------------------------------------------
# The `expected` half
# --------------------------------------------------------------------------


async def test_a_short_upstream_does_not_report_full_coverage():
    """**The `expected` half.** The eight-team fixture is a truncated league,
    and the floor is what makes it read as one: `expected` stays 32 while
    `present` is 8. Derived from the fetch it would read `8 / 8`, ratio 1.0 —
    perfectly healthy while 24 defences vanished."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.coverage.expected == EXPECTED_FLOOR[STRENGTH]
    assert envelope.coverage.present == TEAM_COUNT
    assert envelope.coverage.ratio == pytest.approx(TEAM_COUNT / 32)
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_a_total_upstream_outage_reports_zero_not_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0, which is correct for
    a bye week and catastrophic for a pass that captured nothing. So the
    failure envelope has to carry the floor too."""
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(pbp_status=500), lake=lake)
    assert lake.writes, "a failed capture must still write an envelope"
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        assert envelope.coverage.ratio == 0.0
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_the_floor_does_not_cap_a_genuine_count():
    """It raises a short count, never lowers a real one — otherwise a genuine
    expansion past 32 would report dishonestly."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.coverage.expected == max(TEAM_COUNT, EXPECTED_FLOOR[STRENGTH])


# --------------------------------------------------------------------------
# The `present` half
# --------------------------------------------------------------------------


def _participation_without(defenses: set[str]) -> bytes:
    """The charted feed with some defences' pass-rush snaps removed.

    Their run plays stay, so those teams still appear in the schedule and are
    therefore still OWED a row — which is the state a `present` predicate has
    to be able to see.
    """
    built = season_module.build_season()
    kept = [play for play in built.plays if play.defense not in defenses or play.rush]
    return season_module.participation_document(
        season_module.Season(plays=kept, season=built.season)
    )


async def test_a_team_with_no_charted_pass_rush_is_not_present():
    """**The `present` half.** A row of nulls is not a captured defence.

    Seven defences lose their charted snaps entirely. They are still published
    — with nulls and a reason — but they must not count as present. A
    predicate that counted ROWS rather than samples would report 8/8 of the
    truncated league and hide a total charting outage.
    """
    feeds = Feeds(
        bodies={"participation": _participation_without(set(season_module.TEAMS[1:]))}
    )
    envelope = (await run_capture(feeds, lake=SpyLake()))[STRENGTH]

    assert envelope.coverage.present == 1, "only one defence has a pressure sample"
    assert len(envelope.signals) == TEAM_COUNT, (
        "the other seven are still published, with nulls and a reason"
    )
    assert set(envelope.coverage.missing) == set(season_module.TEAMS[1:])
    reasons = {error["reason"] for error in envelope.errors}
    assert "no_charted_pass_rush_snaps_for_this_team" in reasons, reasons


async def test_a_single_missing_team_is_named_in_coverage_missing():
    """`missing` is derived from `expected − present`, so a team that failed
    has to be findable by name rather than only by a count."""
    dropped = season_module.TEAMS[3]
    feeds = Feeds(bodies={"participation": _participation_without({dropped})})
    envelope = (await run_capture(feeds, lake=SpyLake()))[STRENGTH]
    assert dropped in envelope.coverage.missing
    assert envelope.coverage.present == TEAM_COUNT - 1


async def test_a_present_team_is_recorded_only_once():
    """`record` refuses a key that was never expected, and expecting one twice
    would inflate `present` past the teams that exist."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.coverage.present == len(
        {row["team_id"] for row in envelope.signals}
    )


# --------------------------------------------------------------------------
# The deadline
# --------------------------------------------------------------------------


async def test_an_exceeded_deadline_records_the_rest_as_missing():
    """A truncated pass that reports itself truncated is useful; one that
    reports itself complete is not."""
    past = datetime.now(tz=UTC) - timedelta(seconds=1)
    envelope = (await run_capture(Feeds(), lake=SpyLake(), deadline=past))[STRENGTH]
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == EXPECTED_FLOOR[STRENGTH]
    assert "deadline_exceeded" in {error["reason"] for error in envelope.errors}

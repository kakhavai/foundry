"""The coverage predicate — **both halves of it**.

`coverage.expected` must never derive from what a fetch returned, and neither
must `present`. A mutation set that attacks only `expected` scored 34/34 on an
earlier collector and still missed a deleted `present` check, so the two are
tested as a pair here and every test says which half it defends.

The floor is the spec's own clause taken literally: 32 offences x (one unit
row + five starter rows) = 192. That is a case where the clause is *usable* —
unlike `defensive-front`, whose three-unit predicate had to be narrowed
because only one of the three units was sourceable and the ratio would have
been 0.0 forever.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from offensive_line.capture import (
    EXPECTED_FLOOR,
    REASON_NO_PASS_BLOCK_SNAPS,
    REASON_STARTERS_UNIDENTIFIED,
    ROWS_PER_TEAM,
    SIGNAL_TYPES,
    STRENGTH,
    TEAMS_IN_LEAGUE,
)
from offensive_line.ratings import RECORD_UNIT, STARTER_POSITIONS

from . import season as season_module
from .conftest import Feeds, SpyLake, run_capture

TEAM_COUNT = len(season_module.TEAMS)
# Every team but the one whose centre has no depth-chart label.
TEAMS_WITH_FIVE = TEAM_COUNT - 1
PRESENT_ON_A_HEALTHY_PASS = TEAM_COUNT + TEAMS_WITH_FIVE * len(STARTER_POSITIONS)


def test_the_floor_is_the_specs_clause_not_a_team_count():
    """192, not 32. The spec counts rows, and this collector emits six per
    team — so a floor of 32 would report a league that published no starter
    row at all as perfectly covered."""
    assert EXPECTED_FLOOR[STRENGTH] == 192
    assert EXPECTED_FLOOR[STRENGTH] == TEAMS_IN_LEAGUE * ROWS_PER_TEAM
    assert ROWS_PER_TEAM == 1 + len(STARTER_POSITIONS)


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())


# --------------------------------------------------------------------------
# The `expected` half
# --------------------------------------------------------------------------


async def test_a_short_upstream_does_not_report_full_coverage():
    """**The `expected` half.** The eight-team fixture is a truncated league,
    and the floor is what makes it read as one: `expected` stays 192 while
    `present` is 43. Derived from the fetch it would read `43 / 43`, ratio
    1.0 — perfectly healthy while twenty-four offences vanished."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.coverage.expected == EXPECTED_FLOOR[STRENGTH]
    assert envelope.coverage.present == PRESENT_ON_A_HEALTHY_PASS
    assert envelope.coverage.ratio < 1.0
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_a_total_upstream_outage_reports_zero_not_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0, which is correct for
    a bye week and catastrophic for a pass that captured nothing. So the
    failure envelope has to carry the floor too."""
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(status={"pbp": 500}), lake=lake)
    assert lake.writes, "a failed capture must still write an envelope"
    for envelope in lake.writes:
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        assert envelope.coverage.ratio == 0.0
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_the_floor_does_not_cap_a_genuine_count():
    """It raises a short count, never lowers a real one — otherwise a genuine
    expansion past 192 would report dishonestly."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.coverage.expected == max(
        TEAM_COUNT * ROWS_PER_TEAM, EXPECTED_FLOOR[STRENGTH]
    )


async def test_every_team_in_the_schedule_owes_six_keys():
    """`expect` is called on the fact that made a key owed — appearing in the
    schedule — never on the row having been built. Present plus missing is
    therefore the whole observed universe, whatever succeeded."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    observed = envelope.coverage.present + len(envelope.coverage.missing)
    assert observed == TEAM_COUNT * ROWS_PER_TEAM


# --------------------------------------------------------------------------
# The `present` half
# --------------------------------------------------------------------------


def _participation_without(teams: set[str]) -> bytes:
    """The charted feed with some lines' pass-block snaps removed.

    Their run plays stay, so those teams still appear in the schedule and are
    therefore still OWED a row — which is the state a `present` predicate has
    to be able to see.
    """
    built = season_module.build_season()
    kept = [play for play in built.plays if play.offense not in teams or play.rush]
    return season_module.participation_document(
        season_module.Season(plays=kept, season=built.season)
    )


async def test_a_team_with_no_charted_pass_block_is_not_present():
    """**The `present` half.** A row of nulls is not a captured line.

    Seven offences lose their charted snaps entirely. Their unit rows are
    still published — with nulls and a reason — but must not count as present.
    A predicate that counted ROWS rather than samples would report the whole
    truncated league as covered and hide a total charting outage.
    """
    feeds = Feeds(
        bodies={"participation": _participation_without(set(season_module.TEAMS[1:]))}
    )
    envelope = (await run_capture(feeds, lake=SpyLake()))[STRENGTH]

    unit_present = [
        key for key in _present_keys(envelope) if key.endswith(f":{RECORD_UNIT}")
    ]
    assert unit_present == [f"{season_module.TEAMS[0]}:{RECORD_UNIT}"], (
        "only one line has a pressure sample"
    )
    reasons = {error["reason"] for error in envelope.errors}
    assert REASON_NO_PASS_BLOCK_SNAPS in reasons, reasons


async def test_a_team_short_of_five_starters_is_missing_all_five():
    """The spec's clause: fewer than five identified starters is reported in
    `coverage.missing` rather than emitted partially. `DDD`'s centre has no
    depth-chart label, so four of its five slots are known and none is
    published."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    short = season_module.UNLABELLED_TEAM
    for position in STARTER_POSITIONS:
        assert f"{short}:{position}" in envelope.coverage.missing
    assert not [
        row
        for row in envelope.signals
        if row["team_id"] == short and row["record_type"] != RECORD_UNIT
    ]
    # The unit row still publishes: its rates do not depend on anyone's name.
    assert f"{short}:{RECORD_UNIT}" not in envelope.coverage.missing
    reasons = {error["reason"] for error in envelope.errors}
    assert REASON_STARTERS_UNIDENTIFIED in reasons, reasons


async def test_the_unsourceable_fields_are_not_in_the_present_predicate():
    """The other half of the clause-swallowing failure. If an unsourceable
    field entered the predicate, coverage would be zero forever **and** the
    ratio would lose its ability to report a truncated upstream — a total
    outage, a dead join and a half-empty week would all read the same."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    for row in envelope.signals:
        if row["record_type"] == RECORD_UNIT:
            assert row["yards_before_contact_per_carry"] is None
    assert envelope.coverage.present > 0, (
        "a null-by-necessity field has entered the present predicate"
    )


async def test_a_key_is_recorded_only_once():
    """`record` refuses a key that was never expected, and expecting one twice
    would inflate `present` past the rows that exist."""
    envelope = (await run_capture(Feeds(), lake=SpyLake()))[STRENGTH]
    assert envelope.coverage.present == len(set(_present_keys(envelope)))


def _present_keys(envelope) -> list[str]:
    """Reconstruct the present set from `expected − missing`."""
    from offensive_line.ratings import STARTER_POSITIONS as positions

    every = [
        f"{team}:{kind}"
        for team in season_module.TEAMS
        for kind in (RECORD_UNIT, *positions)
    ]
    missing = set(envelope.coverage.missing)
    return [key for key in every if key not in missing]


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

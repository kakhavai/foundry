"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These tests
are the ones that fail if somebody "simplifies" `capture.py` by computing the
expectation from the games it happened to receive.

A schedule has the least excuse of any collector for a soft floor: the league
has a fixed number of clubs, a club plays at most one game a week, the bye
window is a published property of the format, and the postseason bracket is
a constant. So the floor here is week-aware, and that is what lets a bye week
and an outage look different from each other — which they must, because
`Coverage.ratio` reads 1.0 for `0/0`.
"""

import pytest

from schedule_context.capture import (
    BYE_WINDOW,
    LEAGUE_CLUBS,
    MAX_CLUBS_ON_BYE,
    POSTSEASON_RECORDS,
    REST,
    SIGNAL_TYPES,
    SITUATIONAL,
    expected_floor,
    expected_floors,
)

from .conftest import (
    SpyLake,
    game_row,
    round_robin,
    run_capture,
    season_rows,
    to_csv,
)


async def test_a_truncated_upstream_does_not_report_full_coverage():
    """The failure this floor exists for: a feed truncated to one game must
    not yield `expected: 2, present: 2`, ratio 1.0."""
    rows = [row for row in season_rows(weeks=3) if row["week"] in {"1", "2"}]
    keep = [row for row in rows if row["week"] == "1"] + [
        next(row for row in rows if row["week"] == "2")
    ]
    envelopes = await run_capture(SpyLake(), csv=to_csv(keep), week=2)
    for envelope in envelopes.values():
        assert envelope.coverage.expected == LEAGUE_CLUBS
        assert envelope.coverage.present == 2
        assert envelope.coverage.ratio == pytest.approx(2 / 32)
        reasons = {error["reason"] for error in envelope.errors}
        assert "below_expected_floor" in reasons, reasons


async def test_an_empty_week_reports_zero_not_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — which is why the
    floor is not optional. A week the feed returned nothing for reports 0.0."""
    envelopes = await run_capture(SpyLake(), csv=to_csv(season_rows(weeks=1)), week=2)
    for envelope in envelopes.values():
        assert envelope.coverage.expected == LEAGUE_CLUBS
        assert envelope.coverage.present == 0
        assert envelope.coverage.ratio == 0.0


async def test_a_bye_week_and_an_outage_do_not_look_alike():
    """The single most important property of this collector's coverage.

    Week 6 sits in the bye window, so its floor allows for six clubs resting.
    A real bye week — thirteen games, twenty-six records — reaches 1.0. The
    same week with nothing captured reaches 0.0. Both are `expected != 0`, so
    neither can hide behind the `0/0 -> 1.0` rule.
    """
    week = 6
    assert week in BYE_WINDOW
    # Thirteen of the sixteen week-6 games: six clubs are on bye.
    played = round_robin(week)[:13]
    assert len({club for pair in played for club in pair}) == 26
    rows = season_rows(weeks=5) + [
        game_row(week=week, away=away, home=home) for away, home in played
    ]
    bye = await run_capture(SpyLake(), csv=to_csv(rows), week=week)
    for envelope in bye.values():
        assert envelope.coverage.expected == LEAGUE_CLUBS - MAX_CLUBS_ON_BYE == 26
        assert envelope.coverage.present == 26
        assert envelope.coverage.ratio == 1.0

    outage = await run_capture(SpyLake(), csv=to_csv(season_rows(weeks=5)), week=week)
    for envelope in outage.values():
        assert envelope.coverage.expected == 26
        assert envelope.coverage.present == 0
        assert envelope.coverage.ratio == 0.0


async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one.

    Week 19 is a postseason week whose floor is twelve records. A week that
    somehow carries a full slate must report thirty-two, not twelve.
    """
    rows = season_rows(weeks=1) + [
        game_row(week=19, away=away, home=home, game_type="POST")
        for away, home in round_robin(19)
    ]
    envelopes = await run_capture(SpyLake(), csv=to_csv(rows), week=19)
    for envelope in envelopes.values():
        assert expected_floor(19) == POSTSEASON_RECORDS[19] == 12
        assert envelope.coverage.expected == 32
        assert envelope.coverage.ratio == 1.0


def test_every_signal_type_declares_a_floor_for_every_week():
    weeks = list(range(1, 23))
    for week in weeks:
        floors = expected_floors(week)
        assert set(floors) == set(SIGNAL_TYPES)
        assert all(floor >= 1 for floor in floors.values())
    # `all(...)` over an empty range is True; pin the length so a refactor
    # that empties `weeks` cannot make the loop above vacuous.
    assert len(weeks) == 22


def test_the_floor_is_a_property_of_the_week_not_of_a_fetch():
    """Weeks outside the bye window owe the whole league; weeks inside it
    allow for byes; the postseason bracket is a constant."""
    assert expected_floor(1) == LEAGUE_CLUBS
    assert expected_floor(4) == LEAGUE_CLUBS
    assert expected_floor(5) == LEAGUE_CLUBS - MAX_CLUBS_ON_BYE
    assert expected_floor(14) == LEAGUE_CLUBS - MAX_CLUBS_ON_BYE
    assert expected_floor(15) == LEAGUE_CLUBS
    assert expected_floor(18) == LEAGUE_CLUBS
    assert expected_floor(22) == 2
    assert expected_floors(7) == {SITUATIONAL: 26, REST: 26}


def test_the_generated_week_really_is_a_full_round_robin():
    """The fixtures above assert 32 records against a floor of 32, which is
    only meaningful if a generated week actually pairs every club exactly
    once."""
    pairs = round_robin(3)
    assert len(pairs) == 16
    clubs = [club for pair in pairs for club in pair]
    assert len(clubs) == 32
    assert len(set(clubs)) == 32

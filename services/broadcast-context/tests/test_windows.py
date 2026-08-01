"""Window classification and slate arithmetic — the two pure guards.

Every distinction here gets its **own fixture**, deliberately. Two arms of a
guard that look alike (a Thursday that is Thanksgiving versus one that is not;
a week short because a kickoff is missing versus one short because the
document was truncated) are exactly where a test set scores well and still
misses a real bug, because one fixture cannot tell the sides apart.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from broadcast_context.windows import (
    MIN_REG_SEASON_WEEK_GAMES,
    build_slate,
    classify_window,
    is_league_holiday,
    is_primetime,
    thanksgiving,
)

EASTERN = ZoneInfo("America/New_York")


def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)


@dataclass(frozen=True)
class FakeGame:
    game_id: str
    week: int
    game_type: str
    kickoff_at: datetime | None


def game(
    game_id: str,
    *,
    week: int = 1,
    game_type: str = "REG",
    kickoff: datetime | None = et(2026, 9, 13, 13),
) -> FakeGame:
    return FakeGame(
        game_id=game_id,
        week=week,
        game_type=game_type,
        kickoff_at=None if kickoff is None else kickoff.astimezone(UTC),
    )


# --------------------------------------------------------------------------
# classify_window
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kickoff", "expected"),
    [
        # The international morning slot: 09:30 ET, wherever it is played.
        # Keyed on the SLOT, not on `location` — the feed marks a designated
        # home game in London as `location: Home`.
        (et(2026, 10, 4, 9, 30), "intl_early"),
        (et(2026, 9, 13, 13, 0), "sun_early"),
        # 15:00 Sunday is rare and still an early-window game.
        (et(2026, 9, 13, 15, 0), "sun_early"),
        # The boundaries, both of them, at the exact minute.
        (et(2026, 9, 13, 15, 59), "sun_early"),
        (et(2026, 9, 13, 16, 0), "sun_late"),
        (et(2026, 9, 13, 16, 25), "sun_late"),
        (et(2026, 9, 13, 19, 59), "sun_late"),
        (et(2026, 9, 13, 20, 0), "snf"),
        (et(2026, 9, 13, 20, 20), "snf"),
        (et(2026, 9, 14, 20, 15), "mnf"),
        # An MNF doubleheader's early game is still mnf, at 19:00.
        (et(2026, 9, 14, 19, 0), "mnf"),
        (et(2026, 9, 17, 20, 15), "tnf"),
        (et(2026, 12, 19, 17, 0), "sat_special"),
        # Real 2026 rows the spec's enum has no bucket for. See the README.
        (et(2026, 9, 9, 20, 20), "weeknight_special"),
        (et(2026, 11, 25, 20, 0), "weeknight_special"),
    ],
)
def test_each_slot_classifies_to_its_own_window(kickoff, expected):
    assert classify_window(kickoff_eastern=kickoff, game_type="REG") == expected


def test_thanksgiving_beats_thursday_night():
    """The arm a single Thursday fixture cannot see.

    2026-11-26 is a Thursday AND Thanksgiving. Classifying by weekday first
    would file the triple-header as three `tnf` games, losing the fact that
    made the slot worth naming.
    """
    assert (
        classify_window(kickoff_eastern=et(2026, 11, 26, 20, 20), game_type="REG")
        == "holiday"
    )
    # The neighbouring Thursday, same weekday, same time, is `tnf`.
    assert (
        classify_window(kickoff_eastern=et(2026, 11, 19, 20, 20), game_type="REG")
        == "tnf"
    )


def test_black_friday_and_christmas_beat_the_weeknight_bucket():
    """Both are Fridays in 2026, and both would otherwise be weeknight games."""
    assert (
        classify_window(kickoff_eastern=et(2026, 11, 27, 15, 0), game_type="REG")
        == "holiday"
    )
    assert (
        classify_window(kickoff_eastern=et(2026, 12, 25, 13, 0), game_type="REG")
        == "holiday"
    )
    # A non-holiday Friday at the same hour is not.
    assert (
        classify_window(kickoff_eastern=et(2026, 12, 18, 13, 0), game_type="REG")
        == "weeknight_special"
    )


def test_a_christmas_game_on_a_sunday_is_still_holiday():
    """2022's Christmas fell on a Sunday. Holiday outranks the Sunday split."""
    assert (
        classify_window(kickoff_eastern=et(2022, 12, 25, 13, 0), game_type="REG")
        == "holiday"
    )


@pytest.mark.parametrize("game_type", ["WC", "DIV", "CON", "SB"])
def test_every_postseason_game_type_is_playoff(game_type):
    assert (
        classify_window(kickoff_eastern=et(2027, 1, 10, 16, 30), game_type=game_type)
        == "playoff"
    )


def test_a_playoff_game_is_playoff_even_on_a_holiday():
    """`game_type` is checked first, and that ordering is the claim."""
    assert (
        classify_window(kickoff_eastern=et(2026, 12, 25, 13, 0), game_type="WC")
        == "playoff"
    )


def test_a_game_with_no_kickoff_time_has_no_window():
    """The spec's silent failure, refused: it must not fall through to a
    plausible default such as `sun_early`."""
    assert classify_window(kickoff_eastern=None, game_type="REG") is None


# --------------------------------------------------------------------------
# is_primetime
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kickoff", "expected"),
    [
        (et(2026, 9, 13, 20, 0), True),
        (et(2026, 9, 13, 19, 59), False),
        (et(2026, 9, 13, 20, 20), True),
        # An MNF doubleheader's 19:15 opener: `false` under the spec's own
        # threshold while sitting in the `mnf` window. Documented, not fixed.
        (et(2026, 9, 14, 19, 15), False),
        # 20:20 ET in Los Angeles is 17:20 local. Reading the spec's "local"
        # literally would make this false; it is the whole timezone argument.
        (et(2026, 9, 13, 20, 20), True),
    ],
)
def test_primetime_is_the_eastern_clock(kickoff, expected):
    assert is_primetime(kickoff) is expected


def test_primetime_is_null_when_the_kickoff_is_unpublished():
    """`False` would claim the game is not primetime — a fact we do not have."""
    assert is_primetime(None) is None


# --------------------------------------------------------------------------
# thanksgiving / is_league_holiday
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2024, date(2024, 11, 28)),
        (2025, date(2025, 11, 27)),
        (2026, date(2026, 11, 26)),
        # 2029's November starts on a Thursday, which is where an
        # off-by-one week in "fourth Thursday" shows up.
        (2029, date(2029, 11, 22)),
    ],
)
def test_thanksgiving_is_the_fourth_thursday(year, expected):
    assert thanksgiving(year) == expected
    assert expected.weekday() == 3


def test_the_days_around_thanksgiving_are_not_all_holidays():
    assert is_league_holiday(date(2026, 11, 26)) is True
    assert is_league_holiday(date(2026, 11, 27)) is True
    assert is_league_holiday(date(2026, 11, 28)) is False
    assert is_league_holiday(date(2026, 11, 25)) is False


# --------------------------------------------------------------------------
# build_slate
# --------------------------------------------------------------------------


def _full_week(week: int, *, count: int = 14) -> list[FakeGame]:
    """One week's worth of games sharing a kickoff instant.

    The instant advances with the week: two weeks are two different slates,
    and a fixture that reused one date would fold their counts together —
    which is a fixture bug that looks exactly like a slot-key bug.
    """
    sunday_13 = et(2026, 9, 13, 13) + timedelta(days=7 * (week - 1))
    return [game(f"g{week}-{i}", week=week, kickoff=sunday_13) for i in range(count)]


def test_the_slot_count_is_by_kickoff_instant_not_by_window():
    """The Divisional Round is the case that decides this.

    Four playoff games all carry `window_id: playoff` and each is the only
    football on television when it kicks off. Counting by window would report
    `games_in_window: 4` for every one of them, and `is_standalone: false` for
    four standalone games.
    """
    games = [
        game("wc-1", week=19, game_type="WC", kickoff=et(2027, 1, 9, 16, 30)),
        game("wc-2", week=19, game_type="WC", kickoff=et(2027, 1, 9, 20, 0)),
        game("wc-3", week=19, game_type="WC", kickoff=et(2027, 1, 10, 13, 0)),
        game("wc-4", week=19, game_type="WC", kickoff=et(2027, 1, 10, 16, 30)),
    ]
    slate = build_slate(games)
    assert slate.incomplete_weeks == frozenset()
    assert [slate.games_in_window(g) for g in games] == [1, 1, 1, 1]


def test_a_shared_instant_counts_every_game_in_it():
    games = _full_week(1, count=14)
    slate = build_slate(games)
    assert {slate.games_in_window(g) for g in games} == {14}


def test_a_week_with_an_unslotted_game_withholds_every_count_in_that_week():
    """The spec's trap: a partial slate produces a wrong value for EVERY game
    in the window, not only the missing one. The unslotted game could belong
    to any slot in the week, so no slot in the week can be trusted."""
    week_one = [*_full_week(1, count=13), game("tbd", week=1, kickoff=None)]
    week_two = _full_week(2, count=14)
    slate = build_slate([*week_one, *week_two])

    assert slate.incomplete_weeks == frozenset({1})
    assert [slate.games_in_window(g) for g in week_one] == [None] * 14
    # The neighbouring week is untouched — the guard is week-scoped, not
    # season-scoped, so one TBD game does not blank the whole season.
    assert {slate.games_in_window(g) for g in week_two} == {14}


def test_a_short_regular_season_week_is_treated_as_truncated():
    """A different arm of the same guard, and it needs its own fixture.

    Every game here HAS a kickoff, so the first arm sees nothing wrong. Six
    teams on bye is the league maximum, so 32 clubs cannot produce fewer than
    13 games; a shorter week means the document was cut between the publisher
    and here, which `stream_csv_dicts` cannot detect on an uncompressed body.
    """
    short = _full_week(3, count=MIN_REG_SEASON_WEEK_GAMES - 1)
    ok = _full_week(4, count=MIN_REG_SEASON_WEEK_GAMES)
    slate = build_slate([*short, *ok])

    assert slate.incomplete_weeks == frozenset({3})
    assert [slate.games_in_window(g) for g in short] == [None] * len(short)
    assert None not in [slate.games_in_window(g) for g in ok]


def test_a_short_playoff_week_is_not_truncated():
    """The minimum applies to regular-season weeks only. A four-game
    Divisional Round is complete, and flagging it would blank
    `games_in_window` for every playoff game every season."""
    games = [
        game(f"div-{i}", week=20, game_type="DIV", kickoff=et(2027, 1, 16, 16 + i))
        for i in range(4)
    ]
    slate = build_slate(games)
    assert slate.incomplete_weeks == frozenset()


def test_an_empty_slate_reports_no_counts_and_no_false_completeness():
    slate = build_slate([])
    assert slate.counts == {}
    assert slate.incomplete_weeks == frozenset()
    assert slate.incomplete_reasons == {}


def test_a_week_below_its_high_water_mark_is_incomplete():
    """The third arm, and the gap the other two leave.

    Fourteen games, all with kickoffs, above the 13-game floor — so arms (a)
    and (b) both see a healthy week. The baseline says this week has been seen
    holding 15, which is the only cheap evidence that the document was
    truncated.
    """
    games = _full_week(6, count=14)
    slate = build_slate(games, week_high_water={6: 15})

    assert slate.incomplete_weeks == frozenset({6})
    assert slate.incomplete_reasons == {6: "week_shrank"}
    assert [slate.games_in_window(g) for g in games] == [None] * 14


def test_a_week_at_or_above_its_high_water_mark_is_complete():
    """The other arm. A guard firing on any change would blank every week the
    moment a postponed game is rescheduled back in."""
    games = _full_week(6, count=15)
    assert build_slate(games, week_high_water={6: 15}).incomplete_weeks == frozenset()
    assert build_slate(games, week_high_water={6: 14}).incomplete_weeks == frozenset()


def test_the_shrink_arm_describes_a_state_rather_than_an_edge():
    """**R2.** The same short week stays flagged however many passes it lasts.

    Compared against the *previous pass's* count this goes quiet the moment
    the truncation persists — 16 then 14 flags, 14 then 14 does not — and the
    wrong `games_in_window` republishes unflagged on a 24-hour cadence
    indefinitely. The high-water mark is what makes the arm a description of
    the slate rather than of one transition.
    """
    short = _full_week(6, count=14)
    for pass_number in (1, 2, 3):
        slate = build_slate(short, week_high_water={6: 16})
        assert slate.incomplete_reasons == {6: "week_shrank"}, pass_number


def test_no_baseline_makes_the_shrink_arm_inert():
    """A first capture has no baseline and must not invent one."""
    games = _full_week(6, count=14)
    assert build_slate(games).incomplete_weeks == frozenset()
    assert build_slate(games, week_high_water={}).incomplete_weeks == frozenset()
    # A baseline for a DIFFERENT week must not reach this one either.
    assert build_slate(games, week_high_water={7: 99}).incomplete_weeks == frozenset()


def test_each_incompleteness_finding_reports_its_own_reason():
    """Three findings, three operator responses. One shared string erases the
    difference between "wait for the league" and "the transport is broken"."""
    games = [
        *_full_week(1, count=13),
        game("tbd", week=1, kickoff=None),
        *_full_week(2, count=12),
        *_full_week(3, count=14),
    ]
    slate = build_slate(games, week_high_water={3: 16})
    assert slate.incomplete_reasons == {
        1: "unslotted_game",
        2: "below_minimum_games",
        3: "week_shrank",
    }

"""The per-team season chain: rest, byes, travel, body clock, acclimatisation.

This is where this collector is actually wrong or right. The route surface is
`collector_core`'s and is proved there; the coverage floor has its own file.
What is left is arithmetic over a calendar, and its characteristic failure is
silent: every number below is plausible whether or not it is correct.

The spec names the one to watch by name — `days_rest` computed by subtracting
calendar dates — so it is the first thing pinned here.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from schedule_context.adapters.upstream import ScheduledGame, parse_kickoff
from schedule_context.chain import (
    DEFAULT_LAST_REGULAR_WEEK,
    SHORT_WEEK_DAYS,
    SeasonChains,
    consecutive_road_games,
    days_since_timezone_change,
    great_circle_miles,
    origin_venue,
    rest_context,
    team_games,
    travel_context,
)
from schedule_context.venues import TEAM_VENUES, home_venue, resolve_venue

EASTERN = ZoneInfo("America/New_York")


def game(
    *,
    week: int,
    away: str,
    home: str,
    gameday: str,
    gametime: str = "13:00",
    neutral: bool = False,
    stadium: str | None = None,
    game_type: str = "REG",
    season: int = 2026,
) -> ScheduledGame:
    """One upstream row already normalised — the adapter's own parsing is
    exercised in `test_upstream_adapter.py`, so this goes through
    `parse_kickoff` and no further."""
    return ScheduledGame(
        game_id=f"{season}_{week:02d}_{away}_{home}",
        season=season,
        week=week,
        game_type=game_type,
        kickoff_at=parse_kickoff(gameday, gametime),
        home_team=home,
        away_team=away,
        is_neutral_site=neutral,
        stadium_name=stadium or f"{home} Stadium",
    )


def chains(*games) -> SeasonChains:
    return SeasonChains(team_games(list(games)))


def record_for(season: SeasonChains, team: str, week: int):
    return next(r for r in season.chain(team) if r.week == week)


# ── days_rest ─────────────────────────────────────────────────────────────────


def test_days_rest_is_measured_from_kickoffs_not_calendar_dates():
    """The spec's named failure mode, in the form where it flips a flag.

    Monday night 20:15 to Saturday 13:00 is five calendar days and 4.70 real
    ones. Date subtraction reports 5 and `is_short_week` comes out False; the
    clock reports 4.70 and it comes out True. A collector that subtracted
    dates would be silently dropping this club out of every short-week
    analysis.
    """
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-14", gametime="20:15"),
        game(week=2, away="BUF", home="NYJ", gameday="2026-09-19", gametime="13:00"),
    )
    rest = rest_context(season, record_for(season, "BUF", 2))

    # 5 calendar days minus (20:15 - 13:00) = 5 - 0.302 = 4.698 days.
    assert rest.days_rest == pytest.approx(4.6979, abs=1e-3)
    assert rest.days_rest < SHORT_WEEK_DAYS
    assert rest.is_short_week is True


def test_a_monday_night_to_sunday_afternoon_turn_is_not_a_short_week():
    """The same bug inverts here: date subtraction says 6, the clock says
    5.7 — still not short, but only because the boundary is on the right
    side. A collector that rounded would be flipping this one."""
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-14", gametime="20:15"),
        game(week=2, away="BUF", home="NYJ", gameday="2026-09-20", gametime="13:00"),
    )
    rest = rest_context(season, record_for(season, "BUF", 2))
    assert rest.days_rest == pytest.approx(5.6979, abs=1e-3)
    assert rest.is_short_week is False


def test_is_short_week_is_strictly_below_five_days():
    """`days_rest < 5.0`, per the spec. Exactly five is not short."""
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13", gametime="13:00"),
        game(week=2, away="BUF", home="NYJ", gameday="2026-09-18", gametime="13:00"),
    )
    rest = rest_context(season, record_for(season, "BUF", 2))
    assert rest.days_rest == SHORT_WEEK_DAYS
    assert rest.is_short_week is False


def test_days_rest_is_null_in_a_clubs_first_game():
    season = chains(game(week=1, away="NYG", home="BUF", gameday="2026-09-13"))
    rest = rest_context(season, record_for(season, "BUF", 1))
    assert rest.days_rest is None
    assert rest.previous_kickoff_at is None
    assert rest.is_short_week is False


def test_a_postponement_leaves_a_real_gap_and_not_a_phantom_bye():
    """Rest comes from the clock, bye adjacency from the weeks a club is
    listed in. A club that played, then had a game moved, shows a real 14-day
    gap without ever being flagged post-bye."""
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
        game(week=2, away="BUF", home="NYJ", gameday="2026-09-20"),
        # Week 3's game against BUF is listed but its kickoff slipped a
        # fortnight; the club is still scheduled in week 3.
        game(week=3, away="MIA", home="BUF", gameday="2026-10-04"),
    )
    rest = rest_context(season, record_for(season, "BUF", 3))
    assert rest.days_rest == pytest.approx(14.0)
    assert rest.is_post_bye is False


# ── byes ──────────────────────────────────────────────────────────────────────


def test_a_missing_week_is_a_bye_on_both_sides():
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
        game(week=3, away="BUF", home="NYJ", gameday="2026-09-27"),
        game(week=4, away="MIA", home="BUF", gameday="2026-10-04"),
    )
    week_one = rest_context(season, record_for(season, "BUF", 1))
    week_three = rest_context(season, record_for(season, "BUF", 3))

    assert week_one.is_pre_bye is True
    assert week_one.is_post_bye is False
    assert week_three.is_post_bye is True
    assert week_three.is_pre_bye is False


def test_the_final_regular_season_week_is_not_flagged_pre_bye():
    """`is_pre_bye` from a naive "no game next week" would mark the last week
    of every season, and every club eliminated from the postseason."""
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
        game(week=2, away="BUF", home="NYJ", gameday="2026-09-20"),
    )
    assert season.last_regular_week == 2
    assert rest_context(season, record_for(season, "BUF", 2)).is_pre_bye is False


def test_a_postseason_exit_is_not_a_bye():
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
        game(week=19, away="BUF", home="NYJ", gameday="2027-01-10", game_type="POST"),
    )
    rest = rest_context(season, record_for(season, "BUF", 19))
    assert rest.is_pre_bye is False
    assert rest.is_post_bye is False


# ── timezones ─────────────────────────────────────────────────────────────────


def test_a_non_utc_local_kickoff_keeps_both_the_instant_and_the_wall_clock():
    """A 13:05 Pacific kickoff is 20:05 UTC and reads 16:05 to an Eastern
    club's body clock. All three numbers have to be right at once, and the
    only way to get there is an IANA zone rather than an offset."""
    season = chains(
        game(week=1, away="BUF", home="SEA", gameday="2026-09-13", gametime="16:05"),
    )
    record = record_for(season, "BUF", 1)
    assert record.kickoff_at == datetime(2026, 9, 13, 20, 5, tzinfo=UTC)

    travel = travel_context(season, record)
    # The feed publishes Eastern; Seattle's wall clock is three hours behind.
    assert travel.kickoff_local_time == "13:05:00"
    assert travel.timezone_shift_hours == -3
    assert travel.body_clock_offset_hours == pytest.approx(16 + 5 / 60)


def test_the_feeds_eastern_time_survives_the_november_dst_transition():
    """A fixed -05:00 offset is right in November and an hour wrong in
    September; a fixed -04:00 is the reverse. The season crosses the
    transition, so only a real zone gets both."""
    september = parse_kickoff("2026-09-13", "13:00")
    november = parse_kickoff("2026-11-15", "13:00")

    assert september == datetime(2026, 9, 13, 17, 0, tzinfo=UTC)  # EDT, UTC-4
    assert november == datetime(2026, 11, 15, 18, 0, tzinfo=UTC)  # EST, UTC-5
    assert september.astimezone(EASTERN).hour == november.astimezone(EASTERN).hour


def test_arizona_does_not_follow_the_mountain_zone_into_dst():
    """`America/Phoenix` never shifts, so its offset relative to Denver
    changes mid-season. A collector carrying fixed offsets gets one of the two
    halves of the season wrong and nothing says so."""
    september = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
    december = datetime(2026, 12, 13, 20, 0, tzinfo=UTC)
    phoenix, denver = TEAM_VENUES["ARI"], TEAM_VENUES["DEN"]

    assert phoenix.utc_offset_hours(september) == -7
    assert phoenix.utc_offset_hours(december) == -7
    assert denver.utc_offset_hours(september) == -6
    assert denver.utc_offset_hours(december) == -7


def test_an_offset_lookup_refuses_a_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        TEAM_VENUES["BUF"].utc_offset_hours(datetime(2026, 9, 13, 17, 0))


# ── travel ────────────────────────────────────────────────────────────────────


def test_consecutive_home_games_travel_zero_miles():
    season = chains(
        game(week=1, away="NYJ", home="BUF", gameday="2026-09-13"),
        game(week=2, away="MIA", home="BUF", gameday="2026-09-20"),
    )
    travel = travel_context(season, record_for(season, "BUF", 2))
    assert travel.travel_distance_mi == 0.0
    assert travel.travel_direction == "none"
    assert travel.consecutive_road_games == 0


def test_a_first_game_has_no_previous_position_to_measure_from():
    """`null`, not zero. Zero would claim the club was already at the venue,
    and `none` would claim a measured trip with no time shift."""
    season = chains(game(week=1, away="BUF", home="SEA", gameday="2026-09-13"))
    record = record_for(season, "BUF", 1)
    assert origin_venue(season, record) is None
    travel = travel_context(season, record)
    assert travel.travel_distance_mi is None
    assert travel.travel_direction is None


def test_flying_west_and_flying_east_are_signed_correctly():
    """New York is UTC-4 in September and Los Angeles UTC-7, so an eastward
    trip RAISES the offset. Getting this backwards is a sign error nothing
    downstream can detect."""
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
        game(week=2, away="BUF", home="SEA", gameday="2026-09-20"),
        game(week=3, away="SEA", home="BUF", gameday="2026-09-27"),
    )
    westward = travel_context(season, record_for(season, "BUF", 2))
    homeward = travel_context(season, record_for(season, "BUF", 3))

    assert westward.travel_direction == "west"
    assert westward.timezone_shift_hours == -3
    # Home again. The trip is eastward; the shift is measured against the
    # club's OWN zone, so a home game is always zero however far it flew.
    assert homeward.travel_direction == "east"
    assert homeward.timezone_shift_hours == 0
    assert homeward.travel_distance_mi == pytest.approx(2100, abs=100)


def test_a_road_stretch_measures_travel_from_the_previous_venue():
    """A club that plays away in consecutive weeks does not fly home in
    between, so the second trip is Seattle-to-San Francisco, not
    Buffalo-to-San Francisco."""
    season = chains(
        game(week=1, away="BUF", home="SEA", gameday="2026-09-13"),
        game(week=2, away="BUF", home="SF", gameday="2026-09-20"),
    )
    record = record_for(season, "BUF", 2)
    assert origin_venue(season, record) is TEAM_VENUES["SEA"]
    assert consecutive_road_games(season, record) == 2

    travel = travel_context(season, record)
    seattle_to_santa_clara = great_circle_miles(TEAM_VENUES["SEA"], TEAM_VENUES["SF"])
    assert travel.travel_distance_mi == pytest.approx(seattle_to_santa_clara)
    assert seattle_to_santa_clara == pytest.approx(680, abs=25)


def test_a_bye_sends_a_club_home_and_breaks_the_road_stretch():
    """Two Pacific road games either side of a bye are not a road stretch: the
    club went home, so it is neither two-deep on the road nor still
    acclimatised when it comes back."""
    season = chains(
        game(week=1, away="BUF", home="SEA", gameday="2026-09-13"),
        game(week=3, away="BUF", home="SF", gameday="2026-09-27"),
    )
    record = record_for(season, "BUF", 3)
    assert consecutive_road_games(season, record) == 1
    assert days_since_timezone_change(season, record) == 0.0


def test_a_home_game_breaks_the_road_stretch():
    season = chains(
        game(week=1, away="BUF", home="SEA", gameday="2026-09-13"),
        game(week=2, away="NYJ", home="BUF", gameday="2026-09-20"),
        game(week=3, away="BUF", home="SF", gameday="2026-09-27"),
    )
    assert consecutive_road_games(season, record_for(season, "BUF", 3)) == 1


def test_great_circle_distance_is_symmetric_and_zero_for_one_place():
    buffalo, seattle = TEAM_VENUES["BUF"], TEAM_VENUES["SEA"]
    assert great_circle_miles(buffalo, buffalo) == 0.0
    assert great_circle_miles(buffalo, seattle) == pytest.approx(
        great_circle_miles(seattle, buffalo)
    )
    assert great_circle_miles(buffalo, seattle) == pytest.approx(2100, abs=100)


# ── acclimatisation ───────────────────────────────────────────────────────────


def test_a_club_in_its_own_zone_has_nothing_to_acclimatise_to():
    """`None`, not a large number. The field is an acclimatisation proxy for
    clubs that stayed on the road; "217 days since the Bills were last outside
    Eastern" is true and useless."""
    season = chains(
        game(week=1, away="NYJ", home="BUF", gameday="2026-09-13"),
        game(week=2, away="BUF", home="MIA", gameday="2026-09-20"),
    )
    assert days_since_timezone_change(season, record_for(season, "BUF", 2)) is None


def test_arriving_in_a_new_zone_reads_zero():
    season = chains(
        game(week=1, away="NYJ", home="BUF", gameday="2026-09-13"),
        game(week=2, away="BUF", home="SEA", gameday="2026-09-20"),
    )
    assert days_since_timezone_change(season, record_for(season, "BUF", 2)) == 0.0


def test_staying_out_west_accumulates_days_in_the_zone():
    """Two consecutive Pacific road games: the club has been in the zone
    since the first kickoff, so the second reads a full week."""
    season = chains(
        game(week=1, away="BUF", home="SEA", gameday="2026-09-13"),
        game(week=2, away="BUF", home="SF", gameday="2026-09-20"),
    )
    assert days_since_timezone_change(
        season, record_for(season, "BUF", 2)
    ) == pytest.approx(7.0)


def test_going_home_in_between_resets_the_acclimatisation_clock():
    season = chains(
        game(week=1, away="BUF", home="SEA", gameday="2026-09-13"),
        game(week=2, away="NYJ", home="BUF", gameday="2026-09-20"),
        game(week=3, away="BUF", home="SF", gameday="2026-09-27"),
    )
    assert days_since_timezone_change(season, record_for(season, "BUF", 3)) == 0.0


# ── venues ────────────────────────────────────────────────────────────────────


def test_a_neutral_site_resolves_by_stadium_name_not_by_home_club():
    """The feed's `stadium_id` for a neutral row points at the designated home
    club's building. Trusting it fetches Detroit for a game played in Munich —
    plausible numbers, schema-valid, wrong by four thousand miles."""
    venue = resolve_venue(
        home_team="DET", stadium_name="Allianz Arena", is_neutral_site=True
    )
    assert venue is not None
    assert venue.venue_id == "allianz"
    assert venue.country == "DE"
    assert venue is not TEAM_VENUES["DET"]


def test_an_unrecognised_neutral_venue_resolves_to_nothing():
    """`None`, never a fallback: a guessed venue is the failure the coverage
    block exists to make visible."""
    assert (
        resolve_venue(
            home_team="DET", stadium_name="Somewhere Unnamed", is_neutral_site=True
        )
        is None
    )


def test_stadium_names_fold_across_punctuation_and_accents():
    assert resolve_venue(
        home_team="ARI", stadium_name="Neo Química Arena", is_neutral_site=True
    ) is resolve_venue(
        home_team="ARI", stadium_name="neo quimica arena", is_neutral_site=True
    )


def test_an_international_venue_is_flagged_and_shifts_the_body_clock():
    season = chains(
        game(week=1, away="NYJ", home="BUF", gameday="2026-09-13"),
        game(
            week=2,
            away="BUF",
            home="JAX",
            gameday="2026-09-20",
            gametime="09:30",
            neutral=True,
            stadium="Wembley Stadium",
        ),
    )
    travel = travel_context(season, record_for(season, "BUF", 2))
    assert travel.is_international is True
    # 09:30 Eastern is 14:30 in London and still 09:30 on a Buffalo body.
    assert travel.kickoff_local_time == "14:30:00"
    assert travel.timezone_shift_hours == 5
    assert travel.body_clock_offset_hours == pytest.approx(9.5)


def test_both_new_york_clubs_share_one_building():
    """Two clubs with one venue must travel zero miles to play each other; a
    table keyed only by club would say otherwise."""
    assert home_venue("NYG").venue_id == home_venue("NYJ").venue_id
    assert great_circle_miles(home_venue("NYG"), home_venue("NYJ")) == 0.0


# Every distinct `stadium` string the real nflverse table carries on a row
# marked `location: Neutral`, for the seasons that have any (2019 onward),
# plus the international venues already announced for later ones. Transcribed
# from the live document rather than imagined: the first run of this collector
# against 2024 came back with two rows missing because the feed writes
# "Tottenham Stadium" where this table said "Tottenham Hotspur Stadium".
FEED_NEUTRAL_STADIUM_NAMES = (
    "Wembley Stadium",
    "Tottenham Stadium",
    "Tottenham Hotspur Stadium",
    "Twickenham Stadium",
    "Allianz Arena",
    "FC Bayern Munich Stadium",
    "Deutsche Bank Park",
    "Azteca Stadium",
    "Estadio Banorte",
    "Arena Corinthians",
    "Maracana Stadium",
    "Stade de France",
    "Bernabeu",
    "Melbourne Cricket Ground",
    "Rogers Centre",
    "State Farm Stadium",
    "University of Phoenix Stadium",
    "Ford Field",
    "Raymond James Stadium",
    "Hard Rock Stadium",
    "Dolphin Stadium",
    "Lucas Oil Stadium",
    "Mercedes-Benz Superdome",
    "MetLife Stadium",
    "Levi's Stadium",
    "TIAA Bank Stadium",
    "SoFi Stadium",
    "NRG Stadium",
    "U.S. Bank Stadium",
    "Mercedes-Benz Stadium",
    "Allegiant Stadium",
    "Acrisure Stadium",
    "FirstEnergy Stadium",
)


@pytest.mark.parametrize("stadium", FEED_NEUTRAL_STADIUM_NAMES)
def test_every_neutral_site_name_the_feed_uses_resolves(stadium):
    """An unrecognised name is not a crash — it is two missing situational
    rows and a `venue_unresolved` error, which is the right behaviour and a
    poor substitute for having the venue."""
    assert (
        resolve_venue(home_team="DET", stadium_name=stadium, is_neutral_site=True)
        is not None
    ), stadium


def test_a_neutral_game_in_a_league_building_reuses_that_clubs_venue():
    """A Super Bowl or a hurricane relocation is a neutral row at a building
    the league already owns. Restating its coordinates would let the alias
    drift away from the club's own entry."""
    assert (
        resolve_venue(
            home_team="KC", stadium_name="State Farm Stadium", is_neutral_site=True
        )
        is TEAM_VENUES["ARI"]
    )


def test_the_venue_table_covers_every_club_the_league_fields():
    assert len(TEAM_VENUES) == 32
    assert len({venue.timezone for venue in TEAM_VENUES.values()}) >= 4
    assert all(venue.country == "US" for venue in TEAM_VENUES.values())


# ── chain construction ────────────────────────────────────────────────────────


def test_a_game_without_a_kickoff_is_left_out_of_the_chain_but_still_scheduled():
    """It cannot be placed in time, so it cannot join the ordering — but a
    listed game is not a bye, so the week still counts as scheduled."""
    season = chains(
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
        game(week=2, away="BUF", home="NYJ", gameday="2026-09-20", gametime=""),
        game(week=3, away="MIA", home="BUF", gameday="2026-09-27"),
    )
    assert [r.week for r in season.chain("BUF")] == [1, 3]
    assert season.is_scheduled_in("BUF", 2) is True
    assert rest_context(season, record_for(season, "BUF", 3)).is_post_bye is False


def test_the_chain_is_ordered_by_kickoff_not_by_feed_order():
    season = chains(
        game(week=3, away="MIA", home="BUF", gameday="2026-09-27"),
        game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
        game(week=2, away="BUF", home="NYJ", gameday="2026-09-20"),
    )
    assert [r.week for r in season.chain("BUF")] == [1, 2, 3]


def test_every_game_produces_exactly_two_team_records():
    records = team_games(
        [
            game(week=1, away="NYG", home="BUF", gameday="2026-09-13"),
            game(week=1, away="MIA", home="NYJ", gameday="2026-09-13"),
        ]
    )
    assert len(records) == 4
    assert {r.home_away for r in records} == {"home", "away"}
    assert len({r.key for r in records}) == 4


def test_a_season_with_no_regular_season_rows_falls_back_to_the_declared_length():
    """`last_regular_week` is read off the table because the league has run
    16-, 17- and 18-week seasons. A table with no REG rows at all still needs
    a number, and zero would mark every week `is_pre_bye`."""
    season = chains(
        game(week=19, away="BUF", home="NYJ", gameday="2027-01-10", game_type="POST"),
    )
    assert season.last_regular_week == DEFAULT_LAST_REGULAR_WEEK == 18


def test_an_unresolvable_venue_earlier_in_a_road_trip_stops_the_zone_walk():
    """Without the guard the walk would read an offset off `None`. With it,
    acclimatisation is measured only as far back as the chain is actually
    known."""
    season = chains(
        game(
            week=1,
            away="BUF",
            home="LA",
            gameday="2026-09-13",
            neutral=True,
            stadium="Somewhere Unnamed",
        ),
        game(week=2, away="BUF", home="SEA", gameday="2026-09-20"),
    )
    record = record_for(season, "BUF", 2)
    assert season.previous(record).venue is None
    assert days_since_timezone_change(season, record) == 0.0


def test_a_local_time_lookup_refuses_a_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        TEAM_VENUES["SEA"].local_time(datetime(2026, 9, 13, 17, 0))


def test_a_neutral_site_game_puts_both_clubs_on_the_road():
    records = team_games(
        [
            game(
                week=1,
                away="BUF",
                home="JAX",
                gameday="2026-09-13",
                neutral=True,
                stadium="Wembley Stadium",
            )
        ]
    )
    assert len(records) == 2
    assert all(r.home_away == "neutral" for r in records)
    assert all(r.on_the_road for r in records)

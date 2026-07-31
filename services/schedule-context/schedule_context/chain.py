"""The per-team season chain — what the calendar is doing to a team.

Everything this collector emits beyond the game's own identity is
**path-dependent**: how rested a team is, whether it is coming off a bye, how
far it flew and how long it has been in this time zone are all facts about the
games *before* this one. That is why `capture` fetches a whole season and this
module turns it into one ordered chain per team; a week-scoped fetch could not
answer any of it.

Two things here are correctness rather than style.

**`days_rest` is computed from kickoff timestamps, never from calendar dates.**
A Sunday 13:00 game followed by a Thursday 20:20 game is 3.3 days of rest;
date subtraction reports 4, and every short-week effect in the model is
attenuated by exactly the amount that matters. The same bug quietly inverts
for a Monday-night-to-Sunday-afternoon turn. The upstream ships its own
date-derived `away_rest`/`home_rest` columns and this module ignores them.

**Bye adjacency comes from the weeks a team is actually scheduled in, while
rest comes from the clock.** Those are two different questions and conflating
them is the failure the spec names: a postponed game leaves a real 14-day gap
in `days_rest` without being a bye, because the team still had a game listed
in the week it did not play.

Timezone handling is explicit everywhere. Every instant this module touches is
timezone-aware, offsets are resolved from IANA zones at the kickoff instant
(so the November DST transition lands where it really lands), and nothing
assumes UTC on a naive value.
"""

from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

from .adapters.upstream import ScheduledGame
from .venues import LEAGUE_COUNTRY, Venue, home_venue, resolve_venue

# The spec's definition: `is_short_week` is `days_rest < 5.0`. Named rather
# than inlined so the test that pins the boundary reads against the same
# constant the mapping does.
SHORT_WEEK_DAYS = 5.0

# Mean Earth radius in statute miles. Great-circle distance, not driving
# distance: teams fly.
EARTH_RADIUS_MI = 3958.7613

# Fallback for the last week of the regular season when the fetched table
# carries no `REG` rows at all. The observed maximum is preferred because the
# league has run 16-, 17- and 18-week regular seasons and a hardcoded number
# silently mislabels the final week of every other format as `is_pre_bye`.
DEFAULT_LAST_REGULAR_WEEK = 18

HOME, AWAY, NEUTRAL = "home", "away", "neutral"


@dataclass(frozen=True)
class TeamGame:
    """One game from one team's point of view. Two per game."""

    game_id: str
    season: int
    week: int
    team: str
    opponent: str
    home_away: str
    kickoff_at: datetime | None
    venue: Venue | None
    stadium_name: str
    is_regular_season: bool

    @property
    def key(self) -> str:
        """The coverage key. Stable across passes and unique within one — it
        is what appears in `coverage.missing`, so a key that changed between
        passes would make every row look newly missing."""
        return f"{self.game_id}:{self.team}"

    @property
    def on_the_road(self) -> bool:
        """A neutral-site game is not home. The designated home team travels
        to Munich the same as its opponent does."""
        return self.home_away != HOME


def team_games(games: list[ScheduledGame]) -> list[TeamGame]:
    """Flatten games into two team-facing records each.

    The venue is resolved once, here, so every downstream field agrees about
    where a game was played. A neutral-site game resolves by stadium name and
    may resolve to nothing — see `venues.resolve_venue` for why that is `None`
    rather than the home team's stadium.
    """
    records: list[TeamGame] = []
    for game in games:
        venue = resolve_venue(
            home_team=game.home_team,
            stadium_name=game.stadium_name,
            is_neutral_site=game.is_neutral_site,
        )
        for team, opponent, at_home in (
            (game.home_team, game.away_team, True),
            (game.away_team, game.home_team, False),
        ):
            records.append(
                TeamGame(
                    game_id=game.game_id,
                    season=game.season,
                    week=game.week,
                    team=team,
                    opponent=opponent,
                    home_away=(
                        NEUTRAL if game.is_neutral_site else (HOME if at_home else AWAY)
                    ),
                    kickoff_at=game.kickoff_at,
                    venue=venue,
                    stadium_name=game.stadium_name,
                    is_regular_season=game.game_type == "REG",
                )
            )
    return records


class SeasonChains:
    """One season of team-games, indexed for path-dependent lookups.

    Built once per capture pass. Chains hold only games with a kickoff, since
    an unslotted game cannot be placed in time; the scheduled-week sets hold
    every listed game, because a listed game is not a bye even when its
    kickoff is unknown.
    """

    def __init__(self, records: list[TeamGame]) -> None:
        self._chains: dict[str, list[TeamGame]] = {}
        self._weeks: dict[str, set[int]] = {}
        self._last_regular_week = 0

        for record in records:
            self._weeks.setdefault(record.team, set()).add(record.week)
            if record.is_regular_season:
                self._last_regular_week = max(self._last_regular_week, record.week)
            if record.kickoff_at is not None:
                self._chains.setdefault(record.team, []).append(record)

        self._positions: dict[tuple[str, str], int] = {}
        for team, chain in self._chains.items():
            chain.sort(key=lambda record: record.kickoff_at)
            for index, record in enumerate(chain):
                self._positions[(team, record.game_id)] = index

        if self._last_regular_week == 0:
            self._last_regular_week = DEFAULT_LAST_REGULAR_WEEK

    @property
    def last_regular_week(self) -> int:
        return self._last_regular_week

    def chain(self, team: str) -> list[TeamGame]:
        return self._chains.get(team, [])

    def position(self, record: TeamGame) -> int | None:
        """Where a record sits in its team's chain, or `None` if it has no
        kickoff and therefore was never placed."""
        return self._positions.get((record.team, record.game_id))

    def previous(self, record: TeamGame) -> TeamGame | None:
        index = self.position(record)
        if index is None or index == 0:
            return None
        return self.chain(record.team)[index - 1]

    def is_scheduled_in(self, team: str, week: int) -> bool:
        return week in self._weeks.get(team, set())


# ── rest ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RestContext:
    previous_kickoff_at: datetime | None
    days_rest: float | None
    is_short_week: bool
    is_post_bye: bool
    is_pre_bye: bool


def rest_context(season: SeasonChains, record: TeamGame) -> RestContext:
    """Rest and bye adjacency for one team-game.

    `days_rest` is `None` for a team's first game of the season — there is
    nothing to measure from, and a zero would read as no rest at all.
    """
    previous = season.previous(record)
    days_rest: float | None = None
    if previous is not None and record.kickoff_at is not None:
        elapsed = record.kickoff_at - previous.kickoff_at
        days_rest = elapsed.total_seconds() / 86400.0

    week = record.week
    # Bye adjacency is a regular-season question. Outside it, "no game next
    # week" means eliminated, not resting, and flagging that as `is_pre_bye`
    # would put a phantom bye on every team that loses a playoff game.
    in_regular_season = record.is_regular_season and week <= season.last_regular_week
    is_post_bye = (
        in_regular_season
        and week > 1
        and not season.is_scheduled_in(record.team, week - 1)
    )
    is_pre_bye = (
        in_regular_season
        and week < season.last_regular_week
        and not season.is_scheduled_in(record.team, week + 1)
    )

    return RestContext(
        previous_kickoff_at=None if previous is None else previous.kickoff_at,
        days_rest=days_rest,
        is_short_week=days_rest is not None and days_rest < SHORT_WEEK_DAYS,
        is_post_bye=is_post_bye,
        is_pre_bye=is_pre_bye,
    )


# ── travel ────────────────────────────────────────────────────────────────────


def great_circle_miles(origin: Venue, destination: Venue) -> float:
    """Haversine distance in statute miles."""
    lat1, lon1 = radians(origin.latitude), radians(origin.longitude)
    lat2, lon2 = radians(destination.latitude), radians(destination.longitude)
    haversine = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MI * asin(sqrt(haversine))


def _stayed_out(previous: TeamGame, record: TeamGame) -> bool:
    """Whether the team stayed on the road between two consecutive games.

    A team that played away last week and is away again the next week does not
    fly home in between. A bye, or a home game, sends it home — so the stretch
    breaks on either.
    """
    return (
        previous.on_the_road and record.on_the_road and record.week - previous.week == 1
    )


def consecutive_road_games(season: SeasonChains, record: TeamGame) -> int:
    """Length of the current road stretch including this game; 0 at home."""
    if not record.on_the_road:
        return 0
    count = 1
    current = record
    while True:
        previous = season.previous(current)
        if previous is None or not _stayed_out(previous, current):
            return count
        count += 1
        current = previous


def origin_venue(season: SeasonChains, record: TeamGame) -> Venue | None:
    """Where the team travelled from, or `None` if that is not knowable.

    The spec defines `travel_distance_mi` as "great-circle from previous venue
    to this venue", so this is the previous *venue*, taken literally — not a
    model of where the club slept. A club coming home from Seattle therefore
    travels the flight home, and consecutive home games travel zero, which is
    the case the spec calls out.

    The consequence worth naming: a bye between two road games understates the
    trip, because the club really did go home in between. Modelling that would
    be a different definition from the one the generator was promised, so it is
    stated here rather than quietly applied. `days_since_timezone_change` does
    model the home return, because acclimatisation is a question about where
    the club *was*, not about a flight.

    `None` for a club's first game of the season: the pre-season location of a
    roster is not something a schedule can answer, and a zero would claim the
    club was already at the venue.
    """
    previous = season.previous(record)
    return None if previous is None else previous.venue


@dataclass(frozen=True)
class TravelContext:
    kickoff_local_time: str
    timezone_shift_hours: int | None
    body_clock_offset_hours: float | None
    travel_distance_mi: float | None
    travel_direction: str | None
    consecutive_road_games: int
    days_since_timezone_change: float | None
    is_international: bool


def travel_context(season: SeasonChains, record: TeamGame) -> TravelContext:
    """Travel, body clock and acclimatisation for one team-game.

    Requires `record.venue` and `record.kickoff_at`; the caller filters those
    out and records them as missing with a reason, so this is not defensive
    about them.
    """
    venue = record.venue
    kickoff = record.kickoff_at
    assert venue is not None and kickoff is not None

    venue_offset = venue.utc_offset_hours(kickoff)
    home = home_venue(record.team)

    timezone_shift: int | None = None
    body_clock: float | None = None
    if home is not None:
        timezone_shift = round(venue_offset - home.utc_offset_hours(kickoff))
        # "Kickoff local time expressed in the team's home-zone clock": a
        # 13:00 Pacific kickoff reads 16.0 to an Eastern team's body.
        home_local = kickoff.astimezone(ZoneInfo(home.timezone))
        body_clock = home_local.hour + home_local.minute / 60.0

    origin = origin_venue(season, record)
    distance: float | None = None
    direction: str | None = None
    if origin is not None:
        distance = great_circle_miles(origin, venue)
        shift = venue_offset - origin.utc_offset_hours(kickoff)
        # Positive offset delta is eastward: New York is -5 and Los Angeles
        # -8, so flying east raises the offset. `none` means a genuine
        # zero-shift trip; `null` means there was no previous position to
        # measure from at all, and the two must not be conflated.
        direction = "none" if shift == 0 else ("east" if shift > 0 else "west")

    return TravelContext(
        kickoff_local_time=venue.local_time(kickoff),
        timezone_shift_hours=timezone_shift,
        body_clock_offset_hours=body_clock,
        travel_distance_mi=distance,
        travel_direction=direction,
        consecutive_road_games=consecutive_road_games(season, record),
        days_since_timezone_change=days_since_timezone_change(season, record),
        is_international=venue.country != LEAGUE_COUNTRY,
    )


def days_since_timezone_change(season: SeasonChains, record: TeamGame) -> float | None:
    """How long the team has been in this venue's UTC offset.

    `None` when the venue's offset equals the team's home offset: the team is
    in the zone it lives in and there is nothing to acclimatise to. The field
    is an acclimatisation proxy for teams that stayed on the road, and
    reporting "217 days" for a home game would be true and useless.

    Otherwise it is measured from the kickoff of the first game in the current
    unbroken road stretch played in this same offset — `0.0` when this game is
    the team's arrival in the zone. Kickoff-to-kickoff, because when a team
    actually flew is not something a schedule feed knows.
    """
    venue, kickoff = record.venue, record.kickoff_at
    assert venue is not None and kickoff is not None

    home = home_venue(record.team)
    target = venue.utc_offset_hours(kickoff)
    if home is not None and home.utc_offset_hours(kickoff) == target:
        return None

    arrival = record
    while True:
        previous = season.previous(arrival)
        if previous is None or not _stayed_out(previous, arrival):
            break
        if previous.venue is None:
            break
        if previous.venue.utc_offset_hours(previous.kickoff_at) != target:
            break
        arrival = previous

    return (kickoff - arrival.kickoff_at).total_seconds() / 86400.0

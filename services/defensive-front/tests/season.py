"""A synthetic season, built so the tests that read it CAN fail.

**The fixture's structure is the point, and it is the thing this collector's
sibling got wrong twice.** `defense-vs-position` shipped two real defects
invisible under 155 green tests because every game in its fixture was 20-20
and every team carried identical values — and then its *first* fix was
degenerate in a subtler way: varying production by the offence alone means a
correct opponent adjustment removes 100% of the variance and collapses to the
league mean, which is bit-for-bit identical to the constant-valued bug.

So every pressure here is generated as **`f(offence) + g(defence)`**:

    p(pressure) = BASE + OFFENSE_WEAKNESS[offence] + DEFENSE_STRENGTH[defence]

A correct adjustment removes the offence term and leaves the defence term, so
the adjusted column keeps real spread. A self-referential one collapses it to
a constant. The two are then distinguishable, which is the whole requirement.

**The schedule is deliberately unbalanced.** A round robin gives every defence
the same opponent multiset, so `mean_time_to_throw_faced` would be identical
across the league, `sxx` would be zero and the timing guard would refuse to
run — a fixture in which the guard is structurally unable to fire. The five
rounds below give each defence a different opponent mix and a mean faced
release time spanning a genuine range.

Documents are emitted in the **real wire format**, header and all, including
the columns the adapters project away — a fixture carrying only the read
columns cannot catch a projection bug, and `required_columns` validates the
full header.
"""

import csv
import gzip
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

SEASON = 2026

# Eight teams rather than 32. The declared floor is 32 either way, so every
# fixture below reports a coverage shortfall — which is the honest reading of
# an eight-team feed and is what a floor is FOR. A fixture sized to the floor
# would make `below_expected_floor` unreachable.
TEAMS: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")

# Five weeks of the seven-round circle schedule for eight teams. Every team
# plays in every week — an earlier `(i + step) % 8` construction silently left
# two teams idle in week 5, which made the front-rotation window untestable
# for exactly the teams the fixture was churning.
#
# **Five of seven, not all seven.** A COMPLETE round robin gives every defence
# the same opponent set, so `mean_time_to_throw_faced` would be identical
# across the league, `sxx` would be zero and the timing guard would refuse to
# run — a fixture in which the guard is structurally unable to fire. Stopping
# two rounds short leaves each team missing a different pair of opponents,
# which is what gives the faced-release-time column its spread.
WEEKS = 5

SNAPS_PER_GAME = 24
CARRIES_PER_GAME = 18

BASE_PRESSURE = 0.30
# `f(offence)`: how much pressure this offence's line allows, over the base.
OFFENSE_WEAKNESS: dict[str, float] = {
    team: 0.02 * (((index * 3) % 8) - 3.5) for index, team in enumerate(TEAMS)
}
# `g(defence)`: how much pressure this front generates, over the base.
DEFENSE_STRENGTH: dict[str, float] = {
    team: 0.03 * (index - 3.5) for index, team in enumerate(TEAMS)
}
# Release time is a property of the OFFENCE, which is what makes
# `mean_time_to_throw_faced` a schedule fact for the defence.
TIME_TO_THROW: dict[str, float] = {
    team: 2.40 + 0.09 * index for index, team in enumerate(TEAMS)
}
# One snap in this many carries a charted release time, mirroring the real
# feed's 42.8%: a sack, scramble or throwaway has none.
RELEASE_EVERY = 2
# Every n-th pressure converts to a sack, varying by defence so
# `pressure_to_sack_rate` is not a constant.
SACK_EVERY: dict[str, int] = {team: 3 + (index % 3) for index, team in enumerate(TEAMS)}
# Every n-th snap is a blitz, varying by defence so `blitz_rate` does too.
BLITZ_EVERY: dict[str, int] = {
    team: 3 + (index % 4) for index, team in enumerate(TEAMS)
}

# Nine front defenders per team, of which seven are the usual rotation and two
# are backups who appear only late — enough for `front_continuity_index` to be
# something other than 1.0.
FRONT_PER_TEAM = 9
FRONT_ROTATION = 7
# Four defensive backs per snap, so `defense_players` carries eleven ids and
# the front narrowing has something to narrow away.
SECONDARY_PER_TEAM = 4

FRONT_POSITIONS = ("DE", "DT", "NT", "OLB", "ILB", "MLB", "LB", "DL", "DE")


def front_id(team: str, slot: int) -> str:
    return f"00-{TEAMS.index(team):02d}{slot:02d}F0"


def secondary_id(team: str, slot: int) -> str:
    return f"00-{TEAMS.index(team):02d}{slot:02d}S0"


def games(*, season: int = SEASON) -> list[tuple[str, int, str, str]]:
    """`(game_id, week, home, away)` for the whole synthetic season.

    The circle method: team 0 is fixed and the other seven rotate, so every
    round is a perfect matching over all eight teams.
    """
    rotating = len(TEAMS) - 1
    built: list[tuple[str, int, str, str]] = []
    for week in range(1, WEEKS + 1):
        offset = week - 1
        pairs = [(0, 1 + offset % rotating)]
        for step in range(1, len(TEAMS) // 2):
            pairs.append(
                (
                    1 + (offset + step) % rotating,
                    1 + (offset - step) % rotating,
                )
            )
        for home_index, away_index in pairs:
            home, away = TEAMS[home_index], TEAMS[away_index]
            built.append((f"{season}_{week:02d}_{away}_{home}", week, home, away))
    return built


@dataclass
class Play:
    """One synthetic play, in both feeds' terms."""

    game_id: str
    play_id: int
    week: int
    offense: str
    defense: str
    play_type: str
    dropback: bool = False
    sack: bool = False
    rush: bool = False
    rushing_yards: float = 0.0
    rushers: int = 0
    was_pressure: bool = False
    time_to_throw: float | None = None
    defenders: tuple[str, ...] = ()


@dataclass
class Season:
    plays: list[Play] = field(default_factory=list)
    season: int = SEASON


def _pressure_count(offense: str, defense: str, snaps: int) -> int:
    rate = BASE_PRESSURE + OFFENSE_WEAKNESS[offense] + DEFENSE_STRENGTH[defense]
    return max(0, min(snaps, round(snaps * rate)))


def _defenders(defense: str, snap_index: int, week: int) -> tuple[str, ...]:
    """Eleven ids: seven front rotation plus four defensive backs.

    **Turnover is concentrated in the LAST week, on purpose.** The second half
    of the league replaces two of its seven front starters in week 5 only. A
    one-week window therefore reads their "current rotation" as
    `{0,1,2,3,4,7,8}` and scores their continuity low; a three-week window
    still reads `{0..6}` and scores it high. That gap is what makes
    `FRONT_WINDOW_WEEKS` testable behaviourally rather than by reading the
    constant back — and it is the synthetic version of the week-18 rest
    artifact that set the constant to three in the first place.
    """
    slots = list(range(FRONT_ROTATION))
    if week == 4 and snap_index % 3 == 0:
        slots[FRONT_ROTATION - 1] = FRONT_ROTATION
    elif week >= 5 and TEAMS.index(defense) >= len(TEAMS) // 2:
        slots[FRONT_ROTATION - 2] = FRONT_ROTATION
        slots[FRONT_ROTATION - 1] = FRONT_ROTATION + 1
    return tuple(front_id(defense, slot) for slot in slots) + tuple(
        secondary_id(defense, slot) for slot in range(SECONDARY_PER_TEAM)
    )


def build_season(
    *,
    season: int = SEASON,
    timing_confound: float = 0.0,
    defense_release_shift: float = 0.0,
    weeks: Sequence[int] | None = None,
) -> Season:
    """The whole season's plays.

    Two knobs, and they drive **opposite** claims about the adjustment. Both
    exist because a guard that has never been shown to fire, and an adjustment
    that has never been shown to remove anything, are each decorations.

    `timing_confound` makes each offence's release timing raise the pressure
    it concedes: it adds `timing_confound x (release_time − mean)` to the rate
    of every defence facing that offence. **The adjustment must ABSORB this.**
    The confound travels in the offence's own pressure-allowed production,
    which is exactly the leave-one-out yardstick, so the guard should keep
    passing however large the knob is turned. That is the evidence for this
    collector's claim that the timing term is not "missing" — it arrives
    through the opponent's production rather than as a second regressor.

    `defense_release_shift` makes the release timing a defence *faces* track
    that defence's own strength, without moving any offence's aggregate
    production. **The adjustment cannot absorb this**, because there is
    nothing in the yardstick to absorb it with, so the residual slope moves
    and the guard must FIRE. That is the spec's failure mode reproduced.
    """
    mean_release = sum(TIME_TO_THROW.values()) / len(TIME_TO_THROW)
    built = Season(season=season)
    for game_id, week, home, away in games(season=season):
        if weeks is not None and week not in weeks:
            continue
        play_id = 0
        for offense, defense in ((home, away), (away, home)):
            confound = timing_confound * (TIME_TO_THROW[offense] - mean_release)
            rate = (
                BASE_PRESSURE
                + OFFENSE_WEAKNESS[offense]
                + DEFENSE_STRENGTH[defense]
                + confound
            )
            pressures = max(0, min(SNAPS_PER_GAME, round(SNAPS_PER_GAME * rate)))
            for index in range(SNAPS_PER_GAME):
                play_id += 1
                pressure = index < pressures
                built.plays.append(
                    Play(
                        game_id=game_id,
                        play_id=play_id,
                        week=week,
                        offense=offense,
                        defense=defense,
                        play_type="pass",
                        dropback=True,
                        sack=pressure and index % SACK_EVERY[defense] == 0,
                        rushers=(5 if index % BLITZ_EVERY[defense] == 0 else 4),
                        was_pressure=pressure,
                        time_to_throw=(
                            TIME_TO_THROW[offense]
                            + defense_release_shift * DEFENSE_STRENGTH[defense]
                            if index % RELEASE_EVERY == 0
                            else None
                        ),
                        defenders=_defenders(defense, index, week),
                    )
                )
            for index in range(CARRIES_PER_GAME):
                play_id += 1
                # Rushing yardage is also `f(offence) + g(defence)`: the
                # offence sets a baseline and the defence moves it, so the
                # line-yards adjustment has both terms to separate.
                yards = (
                    3.0
                    + 12.0 * OFFENSE_WEAKNESS[offense]
                    - 20.0 * DEFENSE_STRENGTH[defense]
                    + (index % 5)
                    - 1.0
                )
                built.plays.append(
                    Play(
                        game_id=game_id,
                        play_id=play_id,
                        week=week,
                        offense=offense,
                        defense=defense,
                        play_type="run",
                        rush=True,
                        rushing_yards=round(yards),
                        defenders=_defenders(defense, index, week),
                    )
                )
    return built


# --------------------------------------------------------------------------
# play_by_play_<season>.csv.gz
# --------------------------------------------------------------------------

# The columns the adapter requires, plus five it does not read. The extras are
# what make the `columns=` projection observable: an adapter that stopped
# projecting would still pass, but one whose header check silently narrowed
# would not.
PBP_COLUMNS: tuple[str, ...] = (
    "game_id",
    "play_id",
    "season_type",
    "week",
    "posteam",
    "defteam",
    "desc",
    "epa",
    "play_type",
    "qb_dropback",
    "sack",
    "rush_attempt",
    "rushing_yards",
    "yards_gained",
    "two_point_attempt",
    "shotgun",
    "qtr",
)


def _pbp_row(play: Play, season_type: str) -> dict[str, str]:
    return {
        "game_id": play.game_id,
        "play_id": str(play.play_id),
        "season_type": season_type,
        "week": str(play.week),
        "posteam": play.offense,
        "defteam": play.defense,
        "desc": "a play",
        "epa": "0.1",
        "play_type": play.play_type,
        "qb_dropback": "1" if play.dropback else "0",
        "sack": "1" if play.sack else "0",
        "rush_attempt": "1" if play.rush else "0",
        "rushing_yards": f"{play.rushing_yards:g}" if play.rush else "",
        "yards_gained": f"{play.rushing_yards:g}" if play.rush else "0",
        "two_point_attempt": "0",
        "shotgun": "1",
        "qtr": "2",
    }


def _csv(columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> str:
    """Real CSV, through `csv.writer`, not `",".join`.

    **The personnel columns contain commas** — `offense_personnel` is
    `"1 RB, 1 TE, 3 WR"` and the real feed quotes it. A fixture that joined on
    commas would shift every later column by two, which is exactly the bug
    this comment replaces: `number_of_pass_rushers` read `defenders_in_box`,
    so every snap looked like a blitz, `was_pressure` read a formation string
    and no snap was ever a pressure. Every rate was populated, plausible and
    wrong.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([str(row.get(column, "")) for column in columns])
    return buffer.getvalue()


def pbp_document(
    season: Season,
    *,
    extra_rows: Sequence[Mapping[str, str]] = (),
    season_type: str = "REG",
) -> bytes:
    """A gzipped play-by-play body, as the release asset is served."""
    rows = [_pbp_row(play, season_type) for play in season.plays]
    rows.extend(extra_rows)
    return gzip.compress(_csv(PBP_COLUMNS, rows).encode("utf-8"))


# --------------------------------------------------------------------------
# pbp_participation_<season>.csv
# --------------------------------------------------------------------------

PARTICIPATION_COLUMNS: tuple[str, ...] = (
    "nflverse_game_id",
    "old_game_id",
    "play_id",
    "possession_team",
    "offense_formation",
    "offense_personnel",
    "defenders_in_box",
    "defense_personnel",
    "number_of_pass_rushers",
    "players_on_play",
    "offense_players",
    "defense_players",
    "n_offense",
    "n_defense",
    "ngs_air_yards",
    "time_to_throw",
    "was_pressure",
    "route",
    "defense_man_zone_type",
    "defense_coverage_type",
    "offense_positions",
    "defense_positions",
)


def _participation_row(play: Play) -> dict[str, str]:
    return {
        "nflverse_game_id": play.game_id,
        "old_game_id": "20260913",
        "play_id": str(play.play_id),
        "possession_team": play.offense,
        "offense_formation": "SHOTGUN",
        "offense_personnel": "1 RB, 1 TE, 3 WR",
        "defenders_in_box": "6",
        "defense_personnel": "4 DL, 2 LB, 5 DB",
        "number_of_pass_rushers": str(play.rushers),
        "players_on_play": ";".join(play.defenders),
        "offense_players": "",
        "defense_players": ";".join(play.defenders),
        "n_offense": "11",
        "n_defense": "11",
        "ngs_air_yards": "7.2",
        # The real feed writes an empty string, not `NA`, for an unthrown
        # dropback.
        "time_to_throw": (
            f"{play.time_to_throw:.2f}" if play.time_to_throw is not None else ""
        ),
        # **The string "FALSE", which is truthy.** A collector reading this
        # column for truthiness rather than equality reports a 100% pressure
        # rate, and this fixture is what makes that fail.
        "was_pressure": "TRUE" if play.was_pressure else "FALSE",
        "route": "",
        "defense_man_zone_type": "ZONE_COVERAGE",
        "defense_coverage_type": "COVER_3",
        "offense_positions": "",
        "defense_positions": "DE;DT;NT;OLB;ILB;MLB;LB;CB;CB;FS;SS",
    }


def participation_document(
    season: Season,
    *,
    extra_rows: Sequence[Mapping[str, str]] = (),
) -> bytes:
    rows = [_participation_row(play) for play in season.plays]
    rows.extend(extra_rows)
    return _csv(PARTICIPATION_COLUMNS, rows).encode("utf-8")


# --------------------------------------------------------------------------
# players.csv.gz
# --------------------------------------------------------------------------

PLAYERS_COLUMNS: tuple[str, ...] = (
    "gsis_id",
    "display_name",
    "position_group",
    "position",
    "latest_team",
    "jersey_number",
    "status",
    "college_name",
)


def players_document(*, include_saf: bool = True) -> bytes:
    """The roster map, front and secondary.

    `include_saf` adds a player carrying `SAF` — a position code nflverse
    publishes for 345 players and `player-identity`'s `KNOWN_POSITIONS` does
    NOT carry. One such code inside a batch fails all 500 queries server-side,
    so `SENDABLE_POSITIONS` has to sanitise it. The row is a safety, so this
    collector's front filter drops it anyway; the point of the fixture is that
    the guard does not DEPEND on that filter staying narrow.
    """
    rows: list[dict[str, str]] = []
    for team_index, team in enumerate(TEAMS):
        for slot in range(FRONT_PER_TEAM):
            rows.append(
                {
                    "gsis_id": front_id(team, slot),
                    "display_name": f"{team} Front {slot}",
                    "position_group": "DL" if slot < 4 else "LB",
                    "position": FRONT_POSITIONS[slot % len(FRONT_POSITIONS)],
                    "latest_team": team,
                    "jersey_number": str(50 + slot + team_index),
                    "status": "ACT",
                    "college_name": "Somewhere",
                }
            )
        for slot in range(SECONDARY_PER_TEAM):
            rows.append(
                {
                    "gsis_id": secondary_id(team, slot),
                    "display_name": f"{team} Back {slot}",
                    "position_group": "DB",
                    "position": "SAF" if include_saf and slot == 0 else "CB",
                    "latest_team": team,
                    "jersey_number": str(20 + slot),
                    "status": "ACT",
                    "college_name": "Somewhere",
                }
            )
    return gzip.compress(_csv(PLAYERS_COLUMNS, rows).encode("utf-8"))


# --------------------------------------------------------------------------
# injuries_<season>.csv.gz
# --------------------------------------------------------------------------

INJURIES_COLUMNS: tuple[str, ...] = (
    "season",
    "season_type",
    "game_type",
    "team",
    "week",
    "gsis_id",
    "position",
    "full_name",
    "report_primary_injury",
    "report_status",
    "practice_status",
)

# `(team, front slot, report_status)` for the upcoming week. `Questionable` is
# present deliberately: the spec says out or doubtful, and a collector that
# counted questionable would publish it.
ABSENCES: tuple[tuple[str, int, str], ...] = (
    ("AAA", 0, "Out"),
    ("AAA", 1, "Doubtful"),
    ("AAA", 2, "Questionable"),
    ("BBB", 0, "Out"),
    ("CCC", 3, ""),
)


def injuries_document(
    *,
    season: int = SEASON,
    week: int = 6,
    absences: Sequence[tuple[str, int, str]] = ABSENCES,
    secondary: bool = True,
) -> bytes:
    """The injury report for `week`, plus one hurt defensive back.

    `secondary` adds a listed-out cornerback, so a collector that forgot to
    narrow `key_absences` to the FRONT publishes him and fails.
    """
    rows: list[dict[str, str]] = []
    for team, slot, status in absences:
        rows.append(
            {
                "season": str(season),
                "season_type": "REG",
                "game_type": "REG",
                "team": team,
                "week": str(week),
                "gsis_id": front_id(team, slot),
                "position": "DT",
                "full_name": f"{team} Front {slot}",
                "report_primary_injury": "Knee",
                "report_status": status,
                "practice_status": "Did Not Participate In Practice",
            }
        )
    if secondary:
        rows.append(
            {
                "season": str(season),
                "season_type": "REG",
                "game_type": "REG",
                "team": "AAA",
                "week": str(week),
                "gsis_id": secondary_id("AAA", 1),
                "position": "CB",
                "full_name": "AAA Back 1",
                "report_primary_injury": "Ankle",
                "report_status": "Out",
                "practice_status": "Did Not Participate In Practice",
            }
        )
    # A postseason row for the same week, which must be filtered out.
    rows.append(
        {
            "season": str(season),
            "season_type": "POST",
            "game_type": "WC",
            "team": "DDD",
            "week": str(week),
            "gsis_id": front_id("DDD", 0),
            "position": "DE",
            "full_name": "DDD Front 0",
            "report_primary_injury": "Hamstring",
            "report_status": "Out",
            "practice_status": "Did Not Participate In Practice",
        }
    )
    return gzip.compress(_csv(INJURIES_COLUMNS, rows).encode("utf-8"))

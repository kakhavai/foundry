"""A synthetic season, built so the tests that read it CAN fail.

**The fixture's structure is the point, and it is the thing this collector's
sibling got wrong twice.** `defense-vs-position` shipped two real defects
invisible under 155 green tests because every game in its fixture was 20-20
and every team carried identical values — and then its *first* fix was
degenerate in a subtler way: varying production by the offence alone means a
correct opponent adjustment removes 100% of the variance and collapses to the
league mean, which is bit-for-bit identical to the constant-valued bug.

So every pressure here is generated as **`f(offence) + g(defence)`**:

    p(pressure) = BASE + LINE_WEAKNESS[offence] + FRONT_STRENGTH[defence]

A correct adjustment removes the *front* term and leaves the *line* term — the
mirror of `defensive-front`, whose adjustment removes the line term and leaves
the front term. A self-referential one collapses the adjusted column to a
constant. The two are then distinguishable, which is the whole requirement.

**The schedule is deliberately unbalanced.** A complete round robin gives every
line the same opponent multiset, so every `opponent_pressure_strength_index`
would be identical and the adjustment would be a constant division that no
test could tell from no adjustment at all. Five of the seven rounds leave each
team missing a different pair of opponents.

**The personnel are built so both arms of the lineup guard are reachable.**
Four teams never change their five (continuity 4, `lineup_changed` false). The
other four bench their left tackle for weeks 3 and 4 and restore him in week 5
— so week 5's hash differs from week 4's (`lineup_changed` true,
`continuity_games` 0) *and* the man who came out has a genuinely **measured**
replacement delta, because the with/without split is 3 games against 2 and
`MIN_DELTA_GAMES` is 2. A fixture where every change fell back to a prior
could not tell a measured delta from a modelled one.

One further team (`DDD`) is deliberately missing a depth-chart label for its
centre, so "fewer than five identified starters goes to `coverage.missing`
rather than being emitted partially" is exercised rather than asserted.

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
from datetime import date, timedelta

SEASON = 2026

# Eight teams rather than 32. The declared floor is 192 either way, so every
# fixture below reports a coverage shortfall — which is the honest reading of
# an eight-team feed and is what a floor is FOR. A fixture sized to the floor
# would make `below_expected_floor` unreachable.
TEAMS: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")

# Five of the seven rounds of a circle schedule. See the module docstring.
WEEKS = 5

# Teams from this index up bench their left tackle in weeks 3 and 4.
CHURN_FROM = 4
CHURN_WEEKS = frozenset({3, 4})

# The team whose centre has no depth-chart label, so it can only ever identify
# four of five starters.
UNLABELLED_TEAM = "DDD"
UNLABELLED_SLOT = 2  # C

# A team that swaps its two guards from `SWAP_FROM_WEEK` — the same five men,
# two of them on the other side of the centre. Common in the sport, and the
# only thing in this fixture that makes the depth chart's **per-week** lookup
# observable: every weekly snapshot otherwise carries identical labels, so a
# collector that read the newest snapshot for every week would be right by
# accident. Under the correct lookup this team's continuity streak is
# `WEEKS - SWAP_FROM_WEEK`; under the newest-snapshot shortcut it is
# `WEEKS - 1`, because all five weeks would share one labelling.
SWAP_TEAM = "BBB"
SWAP_FROM_WEEK = 4
SWAP_SLOTS = (1, 3)  # LG and RG

# **The swap is published on the game-day snapshot of `SWAP_FROM_WEEK`**, not
# before it. That single choice is what makes two documented defences
# testable at once: a lookup that stops short of game day (`bisect_left`) and
# a `week_dates` that takes the week's *earliest* game both read the
# pre-swap chart for that week, and both then report `SWAP_TEAM` as having
# changed its five in week 5 when it did not.
SWAP_FROM_SNAPSHOT = 2  # index into `chart_dates`: 0 Tue, 1 Sat, 2 game day

# A team whose left tackle is a genuine rotation: **three** men on identical
# snap counts, every week. Nothing in the snap feed separates them, so the tie
# has to break on something stable — the id — and never on the order the
# upstream happened to write its rows in.
#
# **Three, not two, and the emission order is chosen rather than convenient.**
# With two tied men there is only one ordering axis, so "highest id" coincides
# with either "first row seen" or "last row seen" whichever way they are
# emitted — and a mutation resolving the tie by row order then survives half
# the time for free. Measured: a two-man tie emitted in descending slot order
# let a first-row-wins mutant through, because first-seen happened to be the
# highest id. With three men and the highest id placed in the MIDDLE of the
# emission order, neither row-order rule can reach the right answer.
TIE_TEAM = "CCC"
TIE_SLOTS = (0, 5, 6)
# Emission order for those three. The highest id is `TIE_SLOTS`' last element
# by construction (ids ascend with the slot), so it is placed second here.
TIE_EMISSION_ORDER = (0, 6, 5)

# An eighth roster slot, on the unlabelled team only: listed at centre and
# credited with **zero** offensive snaps. A man who did not play cannot fill
# a slot, and without such a row in the fixture that rule is unfalsifiable.
ZERO_SNAP_SLOT = 7

SNAPS_PER_GAME = 24
CARRIES_PER_GAME = 18

# The team's own offensive snap total per game — the denominator of
# `starter_snap_share`, and the value `snaps.py` derives as the max over every
# player in the team-game.
TEAM_SNAPS_PER_GAME = 70
STARTER_SNAPS = 68
BENCH_SNAPS = 6

BASE_PRESSURE = 0.30
# How much worse the line is in a week the swing man plays left tackle.
# **Non-zero on purpose.** A fixture where replacing a starter changes nothing
# measurable makes every `replacement_delta_pressure_rate` come out at 0.0,
# and a delta of exactly zero is indistinguishable from a correction that was
# never computed — which is precisely what the lineup guard exists to catch.
# The guard would then fire on the fixture's own churn teams, and the only way
# to make the suite green would be to weaken the guard.
SWING_PENALTY = 0.08
# `f(offence)`: how much pressure this line concedes, over the base.
LINE_WEAKNESS: dict[str, float] = {
    team: 0.02 * (((index * 3) % 8) - 3.5) for index, team in enumerate(TEAMS)
}
# `g(defence)`: how much pressure this front generates, over the base.
FRONT_STRENGTH: dict[str, float] = {
    team: 0.03 * (index - 3.5) for index, team in enumerate(TEAMS)
}
# Release time is a property of the OFFENCE, which is what makes
# `mean_time_to_throw` a property of the line's own quarterback.
TIME_TO_THROW: dict[str, float] = {
    team: 2.40 + 0.09 * index for index, team in enumerate(TEAMS)
}
# One dropback in this many carries a charted release time, mirroring the real
# feed's 42.8%: a sack, scramble or throwaway has none.
RELEASE_EVERY = 2
# Every n-th pressure conceded becomes a sack, varying by offence so
# `sack_rate_allowed` is not a fixed multiple of `pressure_rate_allowed`.
SACK_EVERY: dict[str, int] = {team: 3 + (index % 3) for index, team in enumerate(TEAMS)}

# Seven linemen per team: five starters in position order, then two reserves.
LINE_PER_TEAM = 7
STARTER_SLOTS = 5
# The reserve who takes over at left tackle on a churn team.
SWING_SLOT = 5

SLOT_POSITIONS = ("LT", "LG", "C", "RG", "RT")
# The `players.csv` position code per slot. `OT`/`G`/`C` are the coarse codes
# that feed carries — deliberately NOT the slot labels, because `player-
# identity`'s KNOWN_POSITIONS does not contain LT/LG/RG/RT and one unmapped
# code fails all 500 queries in a batch.
SLOT_ROSTER_POSITION = ("OT", "G", "C", "G", "OT")
# The `snap_counts` position code per slot — a third vocabulary for the same
# men, which is why the join runs on ids rather than on positions.
SLOT_SNAP_POSITION = ("T", "G", "C", "G", "T")

# The one lineman on injured reserve, so `starter_availability` can emit `ir`
# — a value the weekly injury report cannot produce at all.
IR_TEAM = "CCC"
IR_SLOT = 3  # RG

# `(team, slot, report_status)` for the upcoming week. `Questionable` is
# present deliberately: it maps to its own enum value rather than to `out`.
REPORTED: tuple[tuple[str, int, str], ...] = (
    ("AAA", 0, "Out"),
    ("BBB", 2, "Questionable"),
    ("EEE", 1, "Doubtful"),
    ("FFF", 4, ""),
    # **The man on injured reserve is ALSO on the game report**, as he is in
    # real life. Roster status has to win: a merge that let the report win
    # would publish `questionable` for a player who cannot be activated, and
    # with no such row in the fixture the two merge orders would agree.
    (IR_TEAM, IR_SLOT, "Questionable"),
)


def line_id(team: str, slot: int) -> str:
    """A GSIS id, in the real feed's `00-00NNNNN` shape."""
    return f"00-1{TEAMS.index(team):02d}{slot:02d}0"


def pfr_id(team: str, slot: int) -> str:
    """A PFR id, in the real feed's `SurnFi01` shape.

    Deliberately unrelated to `line_id`: the two feeds share no key, and a
    fixture whose ids were derivable from one another would let a collector
    join them by string surgery and still pass.
    """
    return f"Ln{TEAMS.index(team):02d}{slot:02d}01"


def skill_id(team: str) -> str:
    """The quarterback, who exists only to set the team's snap total."""
    return f"Qb{TEAMS.index(team):02d}0001"


def _label(team: str, slot: int, swapped: bool) -> str:
    """Which of the five slots one snapshot puts roster `slot` in.

    The **label**, never the membership — `snap_counts` decides who plays.
    `swapped` is a property of the snapshot rather than of the week, so a
    chart published mid-week can differ from the one published before it.
    """
    position = SLOT_POSITIONS[slot % STARTER_SLOTS]
    if swapped and team == SWAP_TEAM and slot in SWAP_SLOTS:
        other = SWAP_SLOTS[1] if slot == SWAP_SLOTS[0] else SWAP_SLOTS[0]
        return SLOT_POSITIONS[other]
    return position


def slot_label(team: str, week: int, slot: int) -> str:
    """The label a correct per-week lookup resolves for `week`.

    The swap lands on the game-day snapshot of `SWAP_FROM_WEEK`, and the
    correct lookup reads the chart current for the week's LATEST game — so it
    sees the swap from that week onward.
    """
    return _label(team, slot, week >= SWAP_FROM_WEEK)


def starting_slot(team: str, week: int, slot_index: int) -> int:
    """Which roster slot occupies `slot_index` in `week`.

    The only place personnel change is expressed, so a test that wants to know
    what the fixture believes reads this rather than reimplementing it.
    """
    if slot_index == 0 and TEAMS.index(team) >= CHURN_FROM and week in CHURN_WEEKS:
        return SWING_SLOT
    return slot_index


def games(*, season: int = SEASON) -> list[tuple[str, int, str, str]]:
    """`(game_id, week, home, away)` for the whole synthetic season.

    The circle method: team 0 is fixed and the other seven rotate, so every
    round is a perfect matching over all eight teams and nobody is idle.
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


# Week 1's Sunday. Real `date` arithmetic from here, not `f"09-{6+7*week}"` —
# that produced `2026-09-34`, which sorts correctly and is not a date. The
# adapter compares dates as strings, so an impossible one would have passed
# every assertion here while proving nothing about a real September/October
# rollover.
SEASON_OPENER = date(SEASON, 9, 13)

# A preseason snapshot, well before week 1.
PRESEASON_CHART_DATE = date(SEASON, 8, 15).isoformat()

# --------------------------------------------------------------------------
# **The calendar's shape is load-bearing, and its first version was
# degenerate.**
#
# It used to be one game date per week with every chart snapshot exactly three
# days earlier, never colliding. Three of this collector's documented defences
# were consequently indistinguishable from their opposites, and one of them —
# reading the chart with `bisect_right` so a snapshot published ON game day
# counts — has a trigger that fires *weekly* against the real feed, which is
# 219 **daily** snapshots against games spread Thursday to Monday.
#
# So: two game days a week, and three chart snapshots a week, one of them on
# game day.
#
#   Tue   chart (pre-week)
#   Thu   the early game            <- one pairing a week kicks off here
#   Sat   chart (mid-week)
#   Sun   chart (game day)  +  the rest of the slate
#
# `week_dates` must resolve to the **Sunday** — the latest game in the week —
# or a Monday-nighter gets labelled from a chart published before the slate.
# The label lookup must include the Sunday chart, not stop before it.
# --------------------------------------------------------------------------

# How many of each week's pairings kick off on the Thursday.
EARLY_GAMES_PER_WEEK = 1


def _sunday(week: int) -> date:
    return SEASON_OPENER + timedelta(days=7 * (week - 1))


def game_date(week: int, *, early: bool = False) -> str:
    """The Sunday of `week`, or its Thursday for the early pairing.

    `early=False` is what `week_dates` must end up holding: the latest game in
    the week, not the first.
    """
    return (_sunday(week) - timedelta(days=3 if early else 0)).isoformat()


def chart_dates(week: int) -> tuple[str, str, str]:
    """`(Tuesday, Saturday, Sunday)` snapshots — the last one on game day.

    Three a week rather than one, and the game-day snapshot is the whole
    point: the real feed republishes daily, so the chart current for a Sunday
    game is routinely one dated that Sunday. A lookup that stopped short of it
    would silently read Saturday's chart every single week.
    """
    sunday = _sunday(week)
    return (
        (sunday - timedelta(days=5)).isoformat(),
        (sunday - timedelta(days=1)).isoformat(),
        sunday.isoformat(),
    )


def chart_date(week: int) -> str:
    """The snapshot that must be current for `week`'s latest game."""
    return chart_dates(week)[-1]


def early_game_ids(*, season: int = SEASON) -> frozenset[str]:
    """The `EARLY_GAMES_PER_WEEK` pairings a week that kick off on Thursday.

    A week with two kickoff dates is what makes "the latest game in the week"
    a different answer from "the first", and therefore what makes the
    `week_dates` defence testable at all.
    """
    seen: dict[int, int] = {}
    early: set[str] = set()
    for game_id, week, _home, _away in games(season=season):
        seen[week] = seen.get(week, 0) + 1
        if seen[week] <= EARLY_GAMES_PER_WEEK:
            early.add(game_id)
    return frozenset(early)


@dataclass
class Play:
    """One synthetic play, in both charted feeds' terms."""

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
    early: bool = False


@dataclass
class Season:
    plays: list[Play] = field(default_factory=list)
    season: int = SEASON


def build_season(
    *,
    season: int = SEASON,
    weeks: Sequence[int] | None = None,
) -> Season:
    """The whole season's plays, as `f(offence) + g(defence)`.

    Both terms are real and separable, which is what lets
    `tests/test_ratings.py` assert that the adjusted column keeps spread
    *and* that the spread it keeps is the line term rather than the front
    term. A fixture varying only one of the two cannot tell a correct
    adjustment from one that collapses to the league mean.
    """
    built = Season(season=season)
    early = early_game_ids(season=season)
    for game_id, week, home, away in games(season=season):
        if weeks is not None and week not in weeks:
            continue
        is_early = game_id in early
        play_id = 0
        for offense, defense in ((home, away), (away, home)):
            benched = starting_slot(offense, week, 0) == SWING_SLOT
            rate = (
                BASE_PRESSURE
                + LINE_WEAKNESS[offense]
                + FRONT_STRENGTH[defense]
                + (SWING_PENALTY if benched else 0.0)
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
                        sack=pressure and index % SACK_EVERY[offense] == 0,
                        rushers=5 if index % 4 == 0 else 4,
                        was_pressure=pressure,
                        time_to_throw=(
                            TIME_TO_THROW[offense]
                            if index % RELEASE_EVERY == 0
                            else None
                        ),
                        early=is_early,
                    )
                )
            for index in range(CARRIES_PER_GAME):
                play_id += 1
                # Rushing yardage is also `f(offence) + g(defence)`: the line
                # sets a baseline and the front moves it, so the line-yards
                # adjustment has both terms to separate.
                yards = (
                    3.0
                    + 12.0 * LINE_WEAKNESS[offense]
                    - 20.0 * FRONT_STRENGTH[defense]
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
                        early=is_early,
                    )
                )
    return built


def _csv(columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> str:
    """Real CSV, through `csv.writer`, not `",".join`.

    **The personnel columns contain commas** — `offense_personnel` is
    `"1 RB, 1 TE, 3 WR"` and the real feed quotes it. A fixture that joined on
    commas would shift every later column by two, which on `defensive-front`
    meant `number_of_pass_rushers` read `defenders_in_box`, so every snap
    looked like a blitz and `was_pressure` read a formation string. Every rate
    was populated, plausible and wrong.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([str(row.get(column, "")) for column in columns])
    return buffer.getvalue()


# --------------------------------------------------------------------------
# play_by_play_<season>.csv.gz
# --------------------------------------------------------------------------

# The columns the adapter requires, plus five it does not read. The extras are
# what make the `columns=` projection observable.
PBP_COLUMNS: tuple[str, ...] = (
    "game_id",
    "play_id",
    "game_date",
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
        "game_date": game_date(play.week, early=play.early),
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


# Charted pass-rush snaps that play-by-play does NOT call dropbacks:
# penalty-nullified `no_play` rows. **5.24% of the real feed's charted rush
# snaps are these**, and they can carry a pressure but can never carry a sack,
# so counting them deflates every sack rate by that much while every field
# stays populated and plausible. Two per team-game here, both charted as
# pressures, so a collector that folded participation without intersecting it
# against play-by-play's dropback index reports a measurably wrong rate.
NO_PLAYS_PER_GAME = 2
NO_PLAY_FIRST_ID = 9800


def no_play_rows(season: Season) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """`(pbp rows, participation rows)` for the nullified snaps.

    Emitted into BOTH documents, which is the point: participation charts them
    as pass rushes with pressure, and play-by-play refuses to call them
    dropbacks. Only the intersection is a pass-block snap.
    """
    pbp_rows: list[dict[str, str]] = []
    part_rows: list[dict[str, str]] = []
    for game_id, week, home, away in games(season=season.season):
        for offset, (offense, defense) in enumerate(((home, away), (away, home))):
            for index in range(NO_PLAYS_PER_GAME):
                play_id = NO_PLAY_FIRST_ID + offset * 10 + index
                pbp_rows.append(
                    {
                        "game_id": game_id,
                        "play_id": str(play_id),
                        "game_date": game_date(week),
                        "season_type": "REG",
                        "week": str(week),
                        "posteam": offense,
                        "defteam": defense,
                        "desc": "penalty; no play",
                        "epa": "0",
                        "play_type": "no_play",
                        "qb_dropback": "0",
                        "sack": "0",
                        "rush_attempt": "0",
                        "rushing_yards": "",
                        "yards_gained": "0",
                        "two_point_attempt": "0",
                        "shotgun": "1",
                        "qtr": "3",
                    }
                )
                part_rows.append(
                    _participation_row(
                        Play(
                            game_id=game_id,
                            play_id=play_id,
                            week=week,
                            offense=offense,
                            defense=defense,
                            play_type="no_play",
                            rushers=4,
                            was_pressure=True,
                            time_to_throw=2.5,
                        )
                    )
                )
    return pbp_rows, part_rows


# One two-point conversion run per team-game. **Not a scrimmage carry**: its
# yardage is not comparable and `defensive-front` excludes it from the same
# denominator, so a collector that counted it would put `run_block_snaps` and
# `run_defense_snaps` on two different play sets and corrupt the differential.
TWO_POINT_YARDS = 2


def _two_point_rows(season: Season) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for game_id, week, home, away in games(season=season.season):
        for offense, defense in ((home, away), (away, home)):
            rows.append(
                {
                    "game_id": game_id,
                    "play_id": "9900",
                    "game_date": game_date(week),
                    "season_type": "REG",
                    "week": str(week),
                    "posteam": offense,
                    "defteam": defense,
                    "desc": "two point attempt",
                    "epa": "0.4",
                    "play_type": "run",
                    "qb_dropback": "0",
                    "sack": "0",
                    "rush_attempt": "1",
                    "rushing_yards": str(TWO_POINT_YARDS),
                    "yards_gained": str(TWO_POINT_YARDS),
                    "two_point_attempt": "1",
                    "shotgun": "0",
                    "qtr": "4",
                }
            )
    return rows


def pbp_document(
    season: Season,
    *,
    extra_rows: Sequence[Mapping[str, str]] = (),
    season_type: str = "REG",
    two_point: bool = True,
    no_play: bool = True,
) -> bytes:
    """A gzipped play-by-play body, as the release asset is served."""
    rows = [_pbp_row(play, season_type) for play in season.plays]
    if season_type == "REG":
        if two_point:
            rows.extend(_two_point_rows(season))
        if no_play:
            rows.extend(no_play_rows(season)[0])
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
    blockers = ";".join(line_id(play.offense, slot) for slot in range(STARTER_SLOTS))
    return {
        "nflverse_game_id": play.game_id,
        "old_game_id": "20260913",
        "play_id": str(play.play_id),
        "possession_team": play.offense,
        "offense_formation": "SHOTGUN",
        # Quoted by `csv.writer` because it contains commas — see `_csv`.
        "offense_personnel": "1 RB, 1 TE, 3 WR",
        "defenders_in_box": "6",
        "defense_personnel": "4 DL, 2 LB, 5 DB",
        "number_of_pass_rushers": str(play.rushers),
        "players_on_play": blockers,
        # Populated deliberately, and deliberately never read: the adapter
        # attributes pressure to the unit, and a fixture that omitted this
        # column would make "we do not read it" untestable.
        "offense_players": blockers,
        "defense_players": "",
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
        "offense_positions": "T;G;C;G;T",
        "defense_positions": "DE;DT;NT;OLB;ILB;MLB;LB;CB;CB;FS;SS",
    }


def participation_document(
    season: Season,
    *,
    extra_rows: Sequence[Mapping[str, str]] = (),
    no_play: bool = True,
) -> bytes:
    rows = [_participation_row(play) for play in season.plays]
    if no_play:
        rows.extend(no_play_rows(season)[1])
    rows.extend(extra_rows)
    return _csv(PARTICIPATION_COLUMNS, rows).encode("utf-8")


# --------------------------------------------------------------------------
# snap_counts_<season>.csv.gz
# --------------------------------------------------------------------------

SNAP_COLUMNS: tuple[str, ...] = (
    "game_id",
    "pfr_game_id",
    "season",
    "game_type",
    "week",
    "player",
    "pfr_player_id",
    "position",
    "team",
    "opponent",
    "offense_snaps",
    "offense_pct",
    "defense_snaps",
    "defense_pct",
    "st_snaps",
    "st_pct",
)


def _snap_row(
    *,
    game_id: str,
    week: int,
    team: str,
    opponent: str,
    slot: int | None,
    snaps: int,
    season: int,
) -> dict[str, str]:
    if slot is None:
        player, identifier, position = f"{team} QB", skill_id(team), "QB"
    else:
        player = f"{team} Line {slot}"
        identifier = pfr_id(team, slot)
        position = SLOT_SNAP_POSITION[slot % STARTER_SLOTS]
    return {
        "game_id": game_id,
        "pfr_game_id": "202609070nor",
        "season": str(season),
        "game_type": "REG",
        "week": str(week),
        "player": player,
        "pfr_player_id": identifier,
        "position": position,
        "team": team,
        "opponent": opponent,
        "offense_snaps": str(snaps),
        "offense_pct": f"{snaps / TEAM_SNAPS_PER_GAME:.2f}",
        "defense_snaps": "0",
        "defense_pct": "0",
        "st_snaps": "3",
        "st_pct": "0.12",
    }


def snap_counts_document(
    *,
    season: int = SEASON,
    weeks: Sequence[int] | None = None,
    drop_players: frozenset[tuple[str, int]] = frozenset(),
) -> bytes:
    """Per-game offensive snaps for the quarterback and every lineman.

    `drop_players` removes `(team, slot)` from every game, which is how a test
    makes a team unable to identify five starters through the *snap* feed
    rather than through the depth chart.
    """
    rows: list[dict[str, str]] = []
    for game_id, week, home, away in games(season=season):
        if weeks is not None and week not in weeks:
            continue
        for team, opponent in ((home, away), (away, home)):
            rows.append(
                _snap_row(
                    game_id=game_id,
                    week=week,
                    team=team,
                    opponent=opponent,
                    slot=None,
                    snaps=TEAM_SNAPS_PER_GAME,
                    season=season,
                )
            )
            starting = {
                starting_slot(team, week, index) for index in range(STARTER_SLOTS)
            }
            extra = [ZERO_SNAP_SLOT] if team == UNLABELLED_TEAM else []
            # **Row order is deliberately not slot order.** The upstream's is
            # its own business, and a tie must not be decided by it — see
            # `TIE_EMISSION_ORDER` for why the tied team's three men are
            # emitted with the highest id in the middle.
            order = sorted([*range(LINE_PER_TEAM), *extra], reverse=True)
            if team == TIE_TEAM:
                rest = [slot for slot in order if slot not in TIE_SLOTS]
                order = [*TIE_EMISSION_ORDER, *rest]
            for slot in order:
                if (team, slot) in drop_players:
                    continue
                if slot == ZERO_SNAP_SLOT:
                    snaps = 0
                elif team == TIE_TEAM and slot in TIE_SLOTS:  # noqa: SIM114
                    # A genuine rotation: nothing in the snap feed separates
                    # these two, so the tie has to break on the id.
                    snaps = STARTER_SNAPS
                elif slot in starting:
                    snaps = STARTER_SNAPS
                else:
                    snaps = BENCH_SNAPS
                rows.append(
                    _snap_row(
                        game_id=game_id,
                        week=week,
                        team=team,
                        opponent=opponent,
                        slot=slot,
                        snaps=snaps,
                        season=season,
                    )
                )
    return gzip.compress(_csv(SNAP_COLUMNS, rows).encode("utf-8"))


# --------------------------------------------------------------------------
# depth_charts_<season>.csv.gz
# --------------------------------------------------------------------------

DEPTH_COLUMNS: tuple[str, ...] = (
    "dt",
    "team",
    "player_name",
    "espn_id",
    "gsis_id",
    "pos_grp_id",
    "pos_grp",
    "pos_id",
    "pos_name",
    "pos_abb",
    "pos_slot",
    "pos_rank",
)

POSITION_NAMES = {
    "LT": "Left Tackle",
    "LG": "Left Guard",
    "C": "Center",
    "RG": "Right Guard",
    "RT": "Right Tackle",
}


def depth_charts_document(
    *,
    season: int = SEASON,
    unlabelled: tuple[str, int] | None = (UNLABELLED_TEAM, UNLABELLED_SLOT),
) -> bytes:
    """One snapshot per week, plus a stale preseason one.

    The preseason snapshot lists a **different** left tackle for every team,
    so an adapter that took the oldest snapshot would mislabel and be caught.
    `SWAP_TEAM` trades its two guards' labels from `SWAP_FROM_WEEK`, so an
    adapter that took the *newest* snapshot for every week is caught too —
    without it every weekly chart carries identical labels and the per-week
    lookup is right by accident. Together they are the synthetic version of
    the real feed's 219 daily snapshots spanning August to March.

    `unlabelled` removes one `(team, slot)` from every snapshot, which is how
    the "fewer than five identified starters" clause is exercised.
    """
    rows: list[dict[str, str]] = []

    def add(date: str, team: str, slot: int, position: str, rank: int) -> None:
        if unlabelled is not None and (team, slot) == unlabelled:
            return
        rows.append(
            {
                "dt": f"{date}T07:32:09Z",
                "team": team,
                "player_name": f"{team} Line {slot}",
                "espn_id": str(4000000 + TEAMS.index(team) * 100 + slot),
                "gsis_id": line_id(team, slot),
                "pos_grp_id": "21",
                "pos_grp": "3WR 1TE",
                "pos_id": str(2 + SLOT_POSITIONS.index(position)),
                "pos_name": POSITION_NAMES[position],
                "pos_abb": position,
                "pos_slot": str(3 + SLOT_POSITIONS.index(position)),
                "pos_rank": str(rank),
            }
        )

    # A preseason chart naming the swing man as the starting left tackle.
    for team in TEAMS:
        for index, position in enumerate(SLOT_POSITIONS):
            slot = SWING_SLOT if index == 0 else index
            add(PRESEASON_CHART_DATE, team, slot, position, 1)

    for week in range(1, WEEKS + 1):
        for index, snapshot in enumerate(chart_dates(week)):
            # The swap is published mid-week, on the game-day snapshot. Every
            # snapshot from that instant onward carries it; the two earlier
            # ones that same week do not. That is what separates "the chart
            # current for the Sunday game" from "the chart published before
            # the slate" and from "Saturday's chart".
            swapped = (week, index) >= (SWAP_FROM_WEEK, SWAP_FROM_SNAPSHOT)
            for team in TEAMS:
                for slot in range(STARTER_SLOTS):
                    add(snapshot, team, slot, _label(team, slot, swapped), 1)
                # The two reserves are listed behind the slots they cover, so
                # a week in which one of them plays still has five labelled
                # slots.
                add(snapshot, team, SWING_SLOT, "LT", 2)
                add(snapshot, team, SWING_SLOT + 1, "LG", 2)
                # **The swing man twice in one snapshot**, at a slot he is
                # ranked lower in. A real chart lists a swing tackle behind
                # both edges; the adapter has to pick his better rank, and
                # doing it by row order instead would make the label depend
                # on how the CSV was written.
                add(snapshot, team, SWING_SLOT, "RT", 3)
                if team == UNLABELLED_TEAM:
                    # The eighth man: listed at centre, and he never plays a
                    # snap. See `ZERO_SNAP_SLOT`.
                    add(snapshot, team, ZERO_SNAP_SLOT, "C", 1)
                if team == TIE_TEAM:
                    # The third man in the left-tackle rotation. Listed at LT
                    # ahead of his generic LG-reserve line, so the lowest-rank
                    # rule puts him at tackle. See `TIE_SLOTS`.
                    add(snapshot, team, TIE_SLOTS[-1], "LT", 1)
    return gzip.compress(_csv(DEPTH_COLUMNS, rows).encode("utf-8"))


# --------------------------------------------------------------------------
# players.csv.gz
# --------------------------------------------------------------------------

PLAYERS_COLUMNS: tuple[str, ...] = (
    "gsis_id",
    "display_name",
    "pfr_id",
    "position_group",
    "position",
    "latest_team",
    "jersey_number",
    "status",
    "college_name",
)


def players_document(
    *,
    drop_crosswalk: frozenset[tuple[str, int]] = frozenset(),
    ir: tuple[str, int] | None = (IR_TEAM, IR_SLOT),
) -> bytes:
    """The roster map: the crosswalk, and the one man on injured reserve.

    `drop_crosswalk` blanks a `(team, slot)`'s `pfr_id`, which is the shape of
    a real crosswalk gap — the row exists, the join key does not.
    """
    rows: list[dict[str, str]] = []
    for team_index, team in enumerate(TEAMS):
        extra = [ZERO_SNAP_SLOT] if team == UNLABELLED_TEAM else []
        for slot in [*range(LINE_PER_TEAM), *extra]:
            position = SLOT_ROSTER_POSITION[slot % STARTER_SLOTS]
            rows.append(
                {
                    "gsis_id": line_id(team, slot),
                    "display_name": f"{team} Line {slot}",
                    "pfr_id": ""
                    if (team, slot) in drop_crosswalk
                    else pfr_id(team, slot),
                    "position_group": "OL",
                    "position": position,
                    "latest_team": team,
                    "jersey_number": str(60 + slot + team_index),
                    "status": "RES" if ir == (team, slot) else "ACT",
                    "college_name": "Somewhere",
                }
            )
        # A skill player, so a collector that forgot to narrow by
        # `position_group` publishes a quarterback as a left tackle.
        rows.append(
            {
                "gsis_id": f"00-2{team_index:02d}0000",
                "display_name": f"{team} QB",
                "pfr_id": skill_id(team),
                "position_group": "QB",
                "position": "QB",
                "latest_team": team,
                "jersey_number": "9",
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


def injuries_document(
    *,
    season: int = SEASON,
    week: int = WEEKS + 1,
    reported: Sequence[tuple[str, int, str]] = REPORTED,
) -> bytes:
    """The injury report for `week`, plus rows that must be filtered out."""
    rows: list[dict[str, str]] = []
    for team, slot, status in reported:
        rows.append(
            {
                "season": str(season),
                "season_type": "REG",
                "game_type": "REG",
                "team": team,
                "week": str(week),
                "gsis_id": line_id(team, slot),
                "position": SLOT_ROSTER_POSITION[slot % STARTER_SLOTS],
                "full_name": f"{team} Line {slot}",
                "report_primary_injury": "Knee",
                "report_status": status,
                "practice_status": "Did Not Participate In Practice",
            }
        )
    # A row for the WRONG week: a collector reading the whole document rather
    # than `week + 1` would mark this man out.
    rows.append(
        {
            "season": str(season),
            "season_type": "REG",
            "game_type": "REG",
            "team": "GGG",
            "week": str(week - 2),
            "gsis_id": line_id("GGG", 0),
            "position": "OT",
            "full_name": "GGG Line 0",
            "report_primary_injury": "Ankle",
            "report_status": "Out",
            "practice_status": "Did Not Participate In Practice",
        }
    )
    # A postseason row for the right week, which must also be filtered out.
    rows.append(
        {
            "season": str(season),
            "season_type": "POST",
            "game_type": "WC",
            "team": "HHH",
            "week": str(week),
            "gsis_id": line_id("HHH", 4),
            "position": "OT",
            "full_name": "HHH Line 4",
            "report_primary_injury": "Hamstring",
            "report_status": "Out",
            "practice_status": "Did Not Participate In Practice",
        }
    )
    return gzip.compress(_csv(INJURIES_COLUMNS, rows).encode("utf-8"))

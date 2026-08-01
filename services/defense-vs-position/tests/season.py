"""A tiny synthetic season, in the real wire format.

The upstreams are two gzipped CSVs and one JSON API, so the fixtures are two
gzipped CSVs and one JSON API. Building them in the real format rather than
monkeypatching the adapters is what keeps the streaming path, the gzip
inflation, the column projection, the header validation and the conditional-GET
tail inside the tests -- every one of which has a documented silent failure
mode, and none of which a patched `fetch_fold` would exercise.

Thirty-two teams and two weeks, which is the smallest shape that can reach the
coverage floor, give the rank guard a full population to rank, and give the
leave-one-out adjustment a second game to leave one out of.
"""

import csv
import gzip
import io
from dataclasses import dataclass, field, replace

from defense_vs_position.adapters import pbp, players

TEAMS: tuple[str, ...] = (
    "ARI",
    "ATL",
    "BAL",
    "BUF",
    "CAR",
    "CHI",
    "CIN",
    "CLE",
    "DAL",
    "DEN",
    "DET",
    "GB",
    "HOU",
    "IND",
    "JAX",
    "KC",
    "LA",
    "LAC",
    "LV",
    "MIA",
    "MIN",
    "NE",
    "NO",
    "NYG",
    "NYJ",
    "PHI",
    "PIT",
    "SEA",
    "SF",
    "TB",
    "TEN",
    "WAS",
)

PBP_COLUMNS: list[str] = sorted(pbp.REQUIRED_COLUMNS)
PLAYERS_COLUMNS: list[str] = sorted(players.REQUIRED_COLUMNS)

# One player per (team, position). Ids are the upstream's own GSIS shape, which
# is what `player-identity` is asked to resolve.
ROSTER_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K")


def gsis_id(team: str, position: str) -> str:
    """A stable synthetic GSIS id. Works for teams outside `TEAMS` too, so an
    expansion fixture does not need a second id scheme."""
    slot = TEAMS.index(team) if team in TEAMS else 90 + (hash(team) % 9)
    return f"00-{slot:02d}{ROSTER_POSITIONS.index(position)}0000"


def _gzip_csv(columns: list[str], rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return gzip.compress(buffer.getvalue().encode("utf-8"))


def players_document(
    *,
    positions: dict[str, str] | None = None,
    extra: list[dict] | None = None,
) -> bytes:
    """`players.csv.gz`, one row per (team, position).

    `positions` overrides a `gsis_id`'s published roster code, which is how a
    test drives the `SENDABLE_POSITIONS` sanitiser and the fantasy-position
    mapping without a second fixture shape.
    """
    overrides = positions or {}
    rows = []
    for team in TEAMS:
        for index, position in enumerate(ROSTER_POSITIONS):
            player = gsis_id(team, position)
            rows.append(
                {
                    "gsis_id": player,
                    "display_name": f"{team} {position}",
                    "position": overrides.get(player, position),
                    "jersey_number": str(10 + index),
                    "latest_team": team,
                }
            )
    rows.extend(extra or [])
    return _gzip_csv(PLAYERS_COLUMNS, rows)


@dataclass
class Play:
    """One play-by-play row, with every required column defaulted to blank."""

    game_id: str
    week: int
    posteam: str
    defteam: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    season_type: str = "REG"
    play_type: str = ""
    values: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        row = {column: "" for column in PBP_COLUMNS}
        row.update(
            game_id=self.game_id,
            season_type=self.season_type,
            week=str(self.week),
            posteam=self.posteam,
            defteam=self.defteam,
            home_team=self.home_team,
            away_team=self.away_team,
            home_score=str(self.home_score),
            away_score=str(self.away_score),
            play_type=self.play_type,
        )
        row.update({key: str(value) for key, value in self.values.items()})
        return row


@dataclass
class Drive:
    """What one offense did to one defense in one game, as knobs.

    Defaults are deliberately boring and identical for every team, so a test
    that changes exactly one knob produces a population in which exactly one
    team differs -- which is what makes a rank-divergence fixture readable.
    """

    completions: int = 4
    receiving_yards: float = 12.0
    yac: float = 5.0
    receiving_tds: int = 1
    incompletions: int = 2
    carries: int = 6
    rushing_yards: float = 4.0
    rushing_tds: int = 1
    sacks: int = 1
    interceptions: int = 0
    field_goals: tuple[float, ...] = (35.0, 47.0)
    extra_points: int = 1
    points: int = 20


def _game_plays(
    game_id: str,
    week: int,
    offense: str,
    defense: str,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    drive: Drive,
) -> list[dict]:
    def play(**values) -> dict:
        return Play(
            game_id=game_id,
            week=week,
            posteam=offense,
            defteam=defense,
            home_team=home,
            away_team=away,
            home_score=home_score,
            away_score=away_score,
            values=values,
            play_type=values.pop("_type", "pass"),
        ).to_row()

    quarterback = gsis_id(offense, "QB")
    receiver = gsis_id(offense, "WR")
    tight_end = gsis_id(offense, "TE")
    back = gsis_id(offense, "RB")
    kicker = gsis_id(offense, "K")

    rows: list[dict] = []
    for index in range(drive.completions):
        target = receiver if index % 2 == 0 else tight_end
        scored = index < drive.receiving_tds
        rows.append(
            play(
                _type="pass",
                passer_player_id=quarterback,
                receiver_player_id=target,
                complete_pass=1,
                passing_yards=drive.receiving_yards,
                receiving_yards=drive.receiving_yards,
                yards_after_catch=drive.yac,
                pass_touchdown=1 if scored else "",
                touchdown=1 if scored else "",
                td_team=offense if scored else "",
            )
        )
    for _ in range(drive.incompletions):
        rows.append(
            play(
                _type="pass", passer_player_id=quarterback, receiver_player_id=receiver
            )
        )
    for _ in range(drive.interceptions):
        rows.append(
            play(
                _type="pass",
                passer_player_id=quarterback,
                receiver_player_id=receiver,
                interception=1,
            )
        )
    for _ in range(drive.sacks):
        rows.append(play(_type="pass", passer_player_id=quarterback, sack=1))
    for index in range(drive.carries):
        scored = index < drive.rushing_tds
        rows.append(
            play(
                _type="run",
                rusher_player_id=back,
                rushing_yards=drive.rushing_yards,
                rush_touchdown=1 if scored else "",
                touchdown=1 if scored else "",
                td_team=offense if scored else "",
            )
        )
    for distance in drive.field_goals:
        rows.append(
            play(
                _type="field_goal",
                kicker_player_id=kicker,
                field_goal_attempt=1,
                field_goal_result="made",
                kick_distance=distance,
            )
        )
    for _ in range(drive.extra_points):
        rows.append(
            play(
                _type="extra_point",
                kicker_player_id=kicker,
                extra_point_attempt=1,
                extra_point_result="good",
            )
        )
    return rows


def pbp_document(
    *,
    weeks: int = 2,
    drives: dict[tuple[str, int], Drive] | None = None,
    teams: tuple[str, ...] = TEAMS,
    extra_rows: list[dict] | None = None,
) -> bytes:
    """`play_by_play_<season>.csv.gz` for a synthetic season.

    `drives` overrides one offense's week by `(offense, week)`, so a test that
    wants one defense to face an unusual opponent changes one entry.
    """
    overrides = drives or {}
    rows: list[dict] = []
    for week in range(1, weeks + 1):
        rotated = _rotation(week, teams)
        for index in range(0, len(rotated) - 1, 2):
            away, home = rotated[index], rotated[index + 1]
            game_id = f"2026_{week:02d}_{away}_{home}"
            away_drive = overrides.get((away, week), Drive())
            home_drive = overrides.get((home, week), Drive())
            rows.extend(
                _game_plays(
                    game_id,
                    week,
                    away,
                    home,
                    home,
                    away,
                    home_drive.points,
                    away_drive.points,
                    away_drive,
                )
            )
            rows.extend(
                _game_plays(
                    game_id,
                    week,
                    home,
                    away,
                    home,
                    away,
                    home_drive.points,
                    away_drive.points,
                    home_drive,
                )
            )
    rows.extend(extra_rows or [])
    return _gzip_csv(PBP_COLUMNS, rows)


def _rotation(week: int, teams: tuple[str, ...] = TEAMS) -> list[str]:
    """The week's pairing order. Rotated so no defense faces the same offense
    twice -- the leave-one-out adjustment is uninteresting otherwise."""
    return [teams[0], *teams[week:], *teams[1:week]]


def opponents_of(team: str, *, weeks: int = 2) -> list[tuple[str, int]]:
    """The `(offense, week)` keys of every offense `team`'s defense faces.

    The join between "a property of one defense" and the `drives` dict, which
    is keyed by the OFFENSE -- a test that wants to change what one defense
    sees has to change every opponent it sees it from.
    """
    keys: list[tuple[str, int]] = []
    for week in range(1, weeks + 1):
        rotated = _rotation(week)
        for index in range(0, len(rotated) - 1, 2):
            away, home = rotated[index], rotated[index + 1]
            if team == away:
                keys.append((home, week))
            elif team == home:
                keys.append((away, week))
    return keys


def volume_skewed(team: str, *, weeks: int = 2) -> dict[tuple[str, int], Drive]:
    """Every offense `team` faces throws far more, at a far lower rate.

    The spec's named failure mode built as **data** rather than asserted as
    arithmetic: a defense that builds leads faces pass-heavy opponents, so its
    per-game allowance inflates while its per-opportunity allowance stays
    elite -- "the raw rating then reads as a soft matchup precisely for the
    teams that are hardest to score against". Here `team`'s opponents throw
    six times as often for a fifth of the yardage, which moves `team` to the
    top of the per-game ranking and the bottom of the per-opportunity one.
    """
    starved = replace(
        Drive(),
        completions=24,
        incompletions=12,
        receiving_yards=2.0,
        yac=1.0,
        receiving_tds=0,
        carries=6,
        rushing_yards=1.0,
        rushing_tds=0,
    )
    return dict.fromkeys(opponents_of(team, weeks=weeks), starved)


def asymmetric_league(*, weeks: int = 2) -> dict[tuple[str, int], Drive]:
    """A league in which **no two teams concede the same thing**.

    The default fixture gives every team 20 points and one sack a game, which
    makes several assertions structurally unable to fail: with a 20-20
    scoreboard, reading `points_scored` off the wrong side of it is a no-op,
    and with every team conceding identically, keying a DST row by the
    opposing defense instead of the conceding team produces byte-identical
    output. Both mutants survived the suite until this fixture existed.

    Two properties are load-bearing and each kills a different mutant:

    * **home and away scores always differ**, so which side of the scoreboard
      `points_scored` was read from is observable;
    * **paired teams concede different sack counts**, so which side of a game
      a DST row was built from is observable.

    **Production depends on BOTH units, and that is the load-bearing part.**
    A first draft varied production by the offense alone. That is a league in
    which defenses have no effect at all, so a *correct* schedule adjustment
    removes 100% of the variance and `adj` collapses to the league mean for
    every team -- which is exactly what the degenerate self-referential
    version produced. The fixture could not tell them apart, and every
    assertion written against it was unfalsifiable.

    So each drive is `f(offense) + g(defense)`: the offensive term is the
    schedule effect an adjustment must remove, the defensive term is the real
    quality it must preserve. `adj` then varies iff the adjustment is doing
    its job, and `opponent_strength_index` stops being perfectly correlated
    with the team's own rate.
    """
    drives: dict[tuple[str, int], Drive] = {}
    for week in range(1, weeks + 1):
        rotation = _rotation(week)
        for index in range(0, len(rotation) - 1, 2):
            away, home = rotation[index], rotation[index + 1]
            for offense, defense, at_home in ((away, home, False), (home, away, True)):
                o = TEAMS.index(offense)
                d = TEAMS.index(defense)
                drives[(offense, week)] = replace(
                    Drive(),
                    # Sacks are GENERATED by the defense, so they key on `d`.
                    # For a DST row this is the schedule effect the adjustment
                    # must remove.
                    sacks=1 + (d % 5),
                    # A team's own score keys on `o` -- its own offensive
                    # quality, which the adjustment must preserve. The +17 for
                    # the away side keeps the two sides of every game unequal:
                    # 3*slot can never close a gap of 17.
                    points=3 * o + (0 if at_home else 17),
                    # Offence + defence, so the five player positions get both
                    # a schedule effect to remove and a defensive signal to
                    # keep.
                    receiving_yards=6.0 + o + 2.0 * d,
                    rushing_yards=2.0 + 0.2 * o + 0.4 * d,
                    field_goals=((35.0,), (35.0, 47.0), (52.0, 22.0, 41.0))[d % 3],
                )
    return drives


def ground_only(team: str, *, weeks: int = 2) -> dict[tuple[str, int], Drive]:
    """Every offense `team` faces runs the ball and nothing else.

    `team`'s defense then has a sampled RB split and an EMPTY QB, WR, TE and K
    split -- rows that exist with `games_sampled == 0`. That is the arm the
    coverage `present` predicate exists for, and it is invisible to a test
    that only attacks the floor.
    """
    run_only = replace(
        Drive(),
        completions=0,
        incompletions=0,
        interceptions=0,
        sacks=0,
        field_goals=(),
        extra_points=0,
    )
    return dict.fromkeys(opponents_of(team, weeks=weeks), run_only)


# Re-exported so a test can build a one-off `Drive` variant without a second
# import line that would drift from this module's own use of it.
__all__ = [name for name in dir() if not name.startswith("_")]

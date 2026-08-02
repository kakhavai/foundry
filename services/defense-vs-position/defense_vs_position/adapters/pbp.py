"""Play-by-play, folded into per-(defense, player, game) counting stats.

The only module that knows nflverse's wire format for this feed.

--------------------------------------------------------------------------
Format: `.csv.gz`, and the freshness check that has to come first
--------------------------------------------------------------------------

Checked live on 2026-08-01 against the releases API, because a size comparison
is worthless if one of the formats has been abandoned -- `player-contract`
shipped against a `.csv.gz` nflverse had stopped rebuilding four years earlier
and published the 2022 offseason as current:

| asset | updated | size |
|---|---|---|
| `play_by_play_2025.csv` | 2026-02-12T09:58:25Z | 93.41 MiB |
| `play_by_play_2025.csv.gz` | 2026-02-12T09:58:23Z | **18.22 MiB** |
| `play_by_play_2025.parquet` | 2026-02-12T09:58:23Z | 19.40 MiB |

All formats share a timestamp, so the freshness exception does not apply and
the fleet-wide size rule does: the parquet is LARGER than the gzipped CSV, and
`pyarrow` is a 47.8 MiB wheel in every collector image. `.csv.gz` with
`gzipped=True`, per `docs/collectors.md`, which also buys the trailer check --
a short body raises `UpstreamTruncated` instead of yielding half a season of
plays that would produce a complete-looking set of ratings with every rate
silently halved.

Projected to 30 of 372 columns.

--------------------------------------------------------------------------
`pbp_participation` is NOT read, and that is a deliberate departure
--------------------------------------------------------------------------

`docs/collectors.md` explicitly leaves this feed unsettled for this collector
("that feed meets the 46.82 MiB question on its own terms, and the answer
there is the field-per-megabyte one"). Answered here, measured:

* Its `route` column is a **single scalar per play** -- the route run by the
  targeted receiver -- verified against the live 2025 artifact. It cannot say
  how many routes each position ran on a snap, so it cannot supply
  `opportunities_defended` as routes-covered. It buys nothing there.
* Its `offense_positions` is the ROSTER-LISTED position of each of the eleven
  players on the field. That is the same fact as `players.csv`'s `position`
  column, repeated once per snap for 45,000 snaps. It is not alignment; see
  `scoring.ALIGNMENTS`.

So the whole 46.82 MiB return is a per-player position map, and
`players.csv.gz` is 2.39 MiB (freshness-checked the same way: every format
stamped 2026-08-01). Reading participation would be 46.82 MiB for information
already held, on a feed that is 69% of the pass's total bytes. Dropped.

--------------------------------------------------------------------------
Memory
--------------------------------------------------------------------------

Folded as rows stream past. Peak is one chunk, one row, the position map
(~8k entries) and the buckets: ~650 players x ~17 games plus 32 x 17 team
lines, a few thousand small dataclasses. The 93.4 MiB inflated document is
never materialised.
"""

import os
from dataclasses import dataclass, field

import httpx
from collector_core.streaming import stream_csv_dicts

from ..ratings import SeasonTotals
from ..scoring import DstLine, StatLine
from .players import PlayerRef

UPSTREAM_ADAPTER = "nflverse-pbp"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "PBP_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.csv.gz",
)

REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "season_type",
        "week",
        "posteam",
        "defteam",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "play_type",
        "sack",
        "complete_pass",
        "passer_player_id",
        "passing_yards",
        "pass_touchdown",
        "interception",
        "receiver_player_id",
        "receiving_yards",
        "yards_after_catch",
        "rusher_player_id",
        "rushing_yards",
        "rush_touchdown",
        "fumbled_1_player_id",
        "fumbled_1_team",
        "fumble_lost",
        "two_point_attempt",
        "two_point_conv_result",
        "field_goal_attempt",
        "field_goal_result",
        "kick_distance",
        "kicker_player_id",
        "extra_point_attempt",
        "extra_point_result",
        "safety",
        "touchdown",
        "td_team",
    }
)

SEASON_TYPE = "REG"

# What counts as an offensive snap for `DstLine.offensive_plays`, the DST
# row's opportunity denominator. Kicks and punts are excluded for the same
# reason `team-scheme` excludes them: they are not plays the offense ran.
OFFENSIVE_PLAY_TYPES = frozenset({"pass", "run"})


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Also the conditional-GET cache key, deliberately the same string, so the
    two cannot drift apart.
    """
    return UPSTREAM_URL_TEMPLATE.format(season=season)


def _num(raw: str) -> float:
    """A numeric cell as a float, with the feed's blank / `NA` as zero.

    Zero rather than `None` is right for every column read through this one:
    they are all counting stats or yardage, and a blank means the play did not
    have that thing. `kick_distance` is the one place a blank would matter and
    it is only ever read on a made field goal, where it is always populated.
    """
    raw = raw.strip()
    if not raw or raw == "NA":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _flag(raw: str) -> bool:
    return _num(raw) == 1.0


@dataclass
class PbpFold:
    """The accumulator, before identity resolution has gated anything.

    Keyed by the upstream's own GSIS id, not by position: a player's
    opportunities are only folded into a position once `player-identity` has
    resolved them, and doing that here would attribute first and resolve
    afterwards.
    """

    # `(defense, gsis_id, game_id) -> StatLine`
    by_player: dict[tuple[str, str, str], StatLine] = field(default_factory=dict)
    # `(conceding team, game_id) -> DstLine`
    dst: dict[tuple[str, str], DstLine] = field(default_factory=dict)
    # `(team, game_id) -> the other team`
    opponents: dict[tuple[str, str], str] = field(default_factory=dict)
    weeks: set[int] = field(default_factory=set)
    # Every GSIS id that recorded at least one opportunity. The resolve set.
    players: set[str] = field(default_factory=set)

    def line(self, defense: str, player_id: str, game_id: str) -> StatLine:
        key = (defense, player_id, game_id)
        line = self.by_player.get(key)
        if line is None:
            line = self.by_player[key] = StatLine()
        line.games.add(game_id)
        self.players.add(player_id)
        return line

    def dst_line(self, team: str, game_id: str) -> DstLine:
        key = (team, game_id)
        line = self.dst.get(key)
        if line is None:
            line = self.dst[key] = DstLine()
        line.games.add(game_id)
        return line


async def fetch_fold(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    positions: dict[str, PlayerRef],
    etag_store=None,
) -> PbpFold:
    """One season's regular-season plays through `week`, folded.

    `positions` gates which players are accumulated at all: a GSIS id with no
    fantasy-eligible roster position never becomes an opportunity, which is
    rule 2 (filter as you parse) applied to the only dimension that has a lot
    of rows to drop.

    Raises on an upstream failure rather than returning a short fold.
    `play_by_play_<season>.csv.gz` does not exist until a season's first games
    are played, so a 404 is the normal state through the offseason and
    `capture` turns it into a `present: 0` envelope rather than a quiet week.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    fold = PbpFold()

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=url,
        **kwargs,
    ):
        if row["season_type"].strip().upper() != SEASON_TYPE:
            continue
        week_raw = row["week"].strip()
        if not week_raw.isdigit():
            continue
        row_week = int(week_raw)
        if row_week > week:
            continue

        offense = row["posteam"].strip()
        defense = row["defteam"].strip()
        game_id = row["game_id"].strip()
        if not offense or not defense or not game_id:
            continue

        fold.weeks.add(row_week)
        fold.opponents[(offense, game_id)] = defense
        fold.opponents[(defense, game_id)] = offense

        _fold_team_defense(fold, row, offense=offense, game_id=game_id)
        _fold_players(
            fold,
            row,
            offense=offense,
            defense=defense,
            game_id=game_id,
            positions=positions,
        )

    return fold


def _fold_team_defense(fold: PbpFold, row: dict, *, offense: str, game_id: str) -> None:
    """What `offense` conceded to the opposing team defense on this play.

    Keyed by the conceding team, which for this one position is the OFFENSE --
    see `DstLine`. Points allowed is the conceding team's own final score,
    recorded per game rather than summed, because the tier ladder is not
    linear and a season total cannot be re-banded.
    """
    line = fold.dst_line(offense, game_id)

    home = row["home_team"].strip()
    if offense == home:
        line.points_scored[game_id] = int(_num(row["home_score"]))
    elif offense == row["away_team"].strip():
        line.points_scored[game_id] = int(_num(row["away_score"]))

    if row["play_type"].strip() in OFFENSIVE_PLAY_TYPES:
        line.offensive_plays += 1
    if _flag(row["sack"]):
        line.sacks += 1
    if _flag(row["interception"]):
        line.interceptions += 1
    if _flag(row["fumble_lost"]):
        line.fumbles_lost += 1
    if _flag(row["safety"]):
        line.safeties += 1
    # A touchdown scored by the side that did NOT have the ball: pick-six,
    # scoop-and-score, punt and kickoff returns. All of them score for the
    # opposing DST and all of them are conceded by `offense`.
    if _flag(row["touchdown"]) and row["td_team"].strip() not in {"", offense}:
        line.defensive_tds += 1


def _fold_players(
    fold: PbpFold,
    row: dict,
    *,
    offense: str,
    defense: str,
    game_id: str,
    positions: dict[str, PlayerRef],
) -> None:
    """Each individual opportunity on this play, attributed to `defense`."""
    two_point = _flag(row["two_point_attempt"])
    two_point_scored = two_point and row["two_point_conv_result"].strip() == "success"

    passer = row["passer_player_id"].strip()
    if passer and passer in positions:
        line = fold.line(defense, passer, game_id)
        # A dropback: an attempt, a sack, or a two-point pass. All three are
        # opportunities the defense defended.
        line.opportunities += 1
        if not two_point:
            if not _flag(row["sack"]):
                line.pass_attempts += 1
            line.passing_yards += _num(row["passing_yards"])
            if _flag(row["pass_touchdown"]):
                line.passing_tds += 1
            if _flag(row["interception"]):
                line.interceptions += 1
        elif two_point_scored:
            line.two_point_conversions += 1

    receiver = row["receiver_player_id"].strip()
    if receiver and receiver in positions:
        line = fold.line(defense, receiver, game_id)
        line.opportunities += 1
        if not two_point:
            line.targets += 1
            if _flag(row["complete_pass"]):
                line.receptions += 1
                line.receiving_yards += _num(row["receiving_yards"])
                line.yards_after_catch += _num(row["yards_after_catch"])
            if _flag(row["pass_touchdown"]):
                line.receiving_tds += 1
        elif two_point_scored:
            line.two_point_conversions += 1

    rusher = row["rusher_player_id"].strip()
    if rusher and rusher in positions:
        line = fold.line(defense, rusher, game_id)
        line.opportunities += 1
        if not two_point:
            line.carries += 1
            line.rushing_yards += _num(row["rushing_yards"])
            if _flag(row["rush_touchdown"]):
                line.rushing_tds += 1
        elif two_point_scored:
            line.two_point_conversions += 1

    # A lost fumble by the side that had the ball. Guarded on `fumbled_1_team`
    # so a defender who fumbles a return is not charged to his own offense's
    # skill players.
    fumbler = row["fumbled_1_player_id"].strip()
    if (
        fumbler
        and fumbler in positions
        and _flag(row["fumble_lost"])
        and row["fumbled_1_team"].strip() == offense
    ):
        fold.line(defense, fumbler, game_id).fumbles_lost += 1

    kicker = row["kicker_player_id"].strip()
    if kicker and kicker in positions:
        if _flag(row["field_goal_attempt"]):
            line = fold.line(defense, kicker, game_id)
            line.opportunities += 1
            if row["field_goal_result"].strip() == "made":
                line.field_goals.append(_num(row["kick_distance"]))
        elif _flag(row["extra_point_attempt"]):
            line = fold.line(defense, kicker, game_id)
            line.opportunities += 1
            if row["extra_point_result"].strip() == "good":
                line.extra_points += 1


def totals_from_fold(
    fold: PbpFold,
    *,
    positions: dict[str, PlayerRef],
    resolved: set[str],
) -> SeasonTotals:
    """Fold player lines into position lines, gated on identity resolution.

    **`resolved` is the gate, and it is applied here rather than earlier on
    purpose.** The spec requires every allowed opportunity to resolve to a
    canonical `player_id` *before* it is attributed to a position; this is
    that boundary. A GSIS id `player-identity` did not resolve contributes
    nothing -- not under its raw upstream id, and not under a candidate
    `player-identity` declined to choose.
    """
    per_game: dict[tuple[str, str, str], StatLine] = {}
    for (defense, player_id, game_id), line in fold.by_player.items():
        if player_id not in resolved:
            continue
        reference = positions.get(player_id)
        if reference is None:
            continue
        key = (defense, reference.fantasy_position, game_id)
        target = per_game.get(key)
        if target is None:
            target = per_game[key] = StatLine()
        target.merge(line)

    return SeasonTotals(
        per_game=per_game,
        dst_per_game=dict(fold.dst),
        opponents=dict(fold.opponents),
        weeks=set(fold.weeks),
    )

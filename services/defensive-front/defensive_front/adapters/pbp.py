"""Play-by-play: the dropback index, and the run-defence fold.

The only module that knows nflverse's wire format for this feed.

--------------------------------------------------------------------------
Format: `.csv.gz`, after the freshness check
--------------------------------------------------------------------------

Re-checked live against the releases API for this build, because a size
comparison is worthless if one of the formats has been abandoned —
`player-contract` shipped against a `.csv.gz` nflverse had stopped rebuilding
four years earlier and published the 2022 offseason as current:

| asset | updated | size |
|---|---|---|
| `play_by_play_2025.csv` | 2026-02-12T09:58:25Z | 93.41 MiB |
| `play_by_play_2025.csv.gz` | 2026-02-12T09:58:23Z | **18.22 MiB** |
| `play_by_play_2025.parquet` | 2026-02-12T09:58:23Z | 19.40 MiB |

All formats share a timestamp, so the freshness exception does not apply and
the fleet-wide size rule does: the parquet is LARGER than the gzipped CSV.
`.csv.gz` with `gzipped=True`, per `docs/collectors.md`, which also buys the
gzip trailer check — a short body raises `UpstreamTruncated` instead of
yielding half a season of plays that would produce a complete-looking set of
ratings with every rate silently halved.

Projected to 12 of 372 columns.

--------------------------------------------------------------------------
Why this adapter builds an index rather than folding pressure itself
--------------------------------------------------------------------------

Pressure is charted in `pbp_participation`, and a pressure counts only on a
play this feed calls a dropback — 5.24% of charted pass-rush snaps are
penalty-nullified `no_play` rows, which can carry a pressure but can never
carry a sack. So the two feeds are intersected, and this one contributes the
side of that intersection it owns: `(game_id, play_id) -> (defence, week,
sack)` for every regular-season dropback in the window.

**The index is built here but joined in `capture`, so the two adapters have no
ordering dependency on each other.** That is deliberate. `team-scheme` folds
its participation feed against an index passed down from its play-by-play
fetch, which means a pass where play-by-play answers `304` and participation
did not folds participation against an **empty** index and silently publishes
zero charted rows — the unconditional re-fetch that follows repairs
play-by-play and never repairs the fold. Reported as a follow-up rather than
fixed from here; this collector avoids the shape entirely.

Measured on the real 2025 season: 48,771 rows in, 19,828 regular-season
dropbacks indexed, 43,868 rows carrying both teams.
"""

import os
from dataclasses import dataclass, field

import httpx
from collector_core.streaming import stream_csv_dicts

from ..ratings import DefenseTotals, OffenseGameTotals, line_yards

UPSTREAM_ADAPTER = "nflverse-pbp"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "PBP_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.csv.gz",
)

REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "play_id",
        "season_type",
        "week",
        "posteam",
        "defteam",
        "play_type",
        "qb_dropback",
        "sack",
        "rush_attempt",
        "rushing_yards",
        "two_point_attempt",
    }
)

SEASON_TYPE = "REG"


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Also the conditional-GET cache key, deliberately the same string, so the
    two cannot drift apart.
    """
    return UPSTREAM_URL_TEMPLATE.format(season=season)


def _num(raw: str) -> float:
    """A numeric cell as a float, with the feed's blank / `NA` as zero.

    Zero rather than `None` is right for every column read through this one:
    they are all flags or yardage, and a blank means the play did not have
    that thing. `rushing_yards` is only read on a play this feed already
    marked `rush_attempt`, where it is populated.
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


@dataclass(frozen=True, slots=True)
class Dropback:
    """One indexed dropback. `slots=True` because there are ~20,000 of them."""

    defense: str
    offense: str
    week: int
    game_id: str
    sack: bool


@dataclass
class PbpFold:
    """The play-by-play half of one pass.

    `dropbacks` is the join key set for `participation`; every other field is
    the run-defence side, which needs no charting and is therefore complete
    even when the charted feed is unavailable.
    """

    dropbacks: dict[tuple[str, int], Dropback] = field(default_factory=dict)
    defense: dict[str, DefenseTotals] = field(default_factory=dict)
    offense_game: dict[tuple[str, str], OffenseGameTotals] = field(default_factory=dict)
    opponents: dict[tuple[str, str], str] = field(default_factory=dict)
    weeks: set[int] = field(default_factory=set)

    def defense_line(self, team: str) -> DefenseTotals:
        line = self.defense.get(team)
        if line is None:
            line = self.defense[team] = DefenseTotals()
        return line

    def offense_line(self, team: str, game_id: str) -> OffenseGameTotals:
        key = (team, game_id)
        line = self.offense_game.get(key)
        if line is None:
            line = self.offense_game[key] = OffenseGameTotals()
        return line


async def fetch_pbp(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> PbpFold:
    """One season's regular-season plays through `week`, folded and indexed.

    Raises on an upstream failure rather than returning a short fold.
    `play_by_play_<season>.csv.gz` does not exist until a season's first games
    are played — verified live: the 2026 artifact 404s today — so a 404 is the
    normal state through the offseason and `capture` turns it into a
    `present: 0` envelope rather than a quiet week.
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
        play_raw = row["play_id"].strip()
        if not play_raw.replace(".", "", 1).isdigit():
            continue
        play_id = int(float(play_raw))

        fold.weeks.add(row_week)
        fold.opponents[(offense, game_id)] = defense
        fold.opponents[(defense, game_id)] = offense

        sack = _flag(row["sack"])
        # A sack IS a dropback; nflverse marks both, but the `or` keeps the
        # index correct if a row ever carries one without the other. Every
        # pressure metric's denominator is this set.
        if _flag(row["qb_dropback"]) or sack:
            fold.dropbacks[(game_id, play_id)] = Dropback(
                defense=defense,
                offense=offense,
                week=row_week,
                game_id=game_id,
                sack=sack,
            )

        # Run defence needs no charting, so it is folded straight here.
        # Two-point attempts are excluded: they are not scrimmage carries and
        # their yardage is not comparable.
        if _flag(row["rush_attempt"]) and not _flag(row["two_point_attempt"]):
            rushing_yards = _num(row["rushing_yards"])
            credited = line_yards(rushing_yards)
            line = fold.defense_line(defense)
            line.carries += 1
            line.line_yards += credited
            if rushing_yards <= 0.0:
                line.stuffs += 1
            allowed = fold.offense_line(offense, game_id)
            allowed.carries += 1
            allowed.line_yards_allowed += credited

    return fold

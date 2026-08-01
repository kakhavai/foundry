"""The play-by-play feed — penalties, aggregated to one counter per game.

This is the expensive upstream and the reason `collector_core.streaming` grew
two options. nflverse publishes the season's play-by-play twice:

| artifact | size | rows x cols |
|---|---|---|
| `play_by_play_2025.csv`    | **93.4 MiB** | 48,771 x 372 |
| `play_by_play_2025.csv.gz` | **18.2 MiB** | same |

Both measured live (2026-08-01), both serving an `ETag` and answering
`If-None-Match` with a `304`.

**This adapter reads the gzipped one, projected to the six columns below.**
Measured over the real 2025 file through the shipped code path:

- streaming `.csv.gz` and building a full 372-key dict per row: **2.8s** CPU
- the same, projected to the six columns below: **1.5s** CPU

Median of three runs each, timed through `stream_csv_dicts` itself against the
real 2025 artifact, with no profiler attached. An earlier revision of this
docstring said 9.8s/3.8s; those runs had `tracemalloc` active, which inflates
allocation-heavy work and inflates the 372-column arm the most.

Identical peak memory either way — the rows are yielded one at a time
regardless, so the projection buys CPU and allocation churn, not headroom. So
the compressed artifact is 5.1x less bandwidth and the projection is a further
1.9x off the CPU. Taking the uncompressed file with the existing helper was the
cheap-to-build option and would have cost 93.4 MiB on every changed poll,
forever, on a collector whose entire product is eight numbers per crew. Both
options went into the shared library rather than this adapter because
`defense-vs-position` reads the same document.

`gzipped=True` also buys a correctness property the uncompressed path cannot
have: a gzip member carries a trailer, so a **short** body raises
`UpstreamTruncated` instead of silently yielding half a season. That matters
more here than the bandwidth — see that exception's docstring for what half a
play-by-play file does to this collector's coverage.

**A penalty here is an ACCEPTED penalty.** nflfastR sets `penalty = 1` on the
play a flag was thrown and enforced; the aggregate lands at 12.72 per game over
the 2025 regular season against a published league average of ~12.7, which is
the check that the flag means what the spec's `penalties_per_game` means
("accepted penalties, both teams"). Declined flags do not appear.

**This upstream is the one most likely to be missing**, and not only through
failure: `play_by_play_<season>.csv.gz` does not exist until a season's first
games are played. `capture.py` therefore treats its absence as a **degraded**
pass rather than a failed one — `game_crew_assignment` still publishes.
"""

import os
from collections import Counter
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-pbp"

# `{season}` is substituted per pass — this feed is one file per season, unlike
# the other two. That also means its ETag key varies by season, which is what
# keeps `ETagStore` bounded: one entry per season captured, not one per poll.
UPSTREAM_URL_TEMPLATE = os.getenv(
    "PBP_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.csv.gz",
)

# Six of 372. See the module docstring for what the other 366 cost.
REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "season_type",
        "play_type",
        "penalty",
        "penalty_type",
        "penalty_yards",
    }
)

SEASON_TYPE = "REG"

# `penalty_type` values, exactly as the feed spells them. Named constants
# because a typo'd literal produces a rate of 0.0 that looks like a crew which
# simply never calls that foul — the quietest possible failure.
DPI = "Defensive Pass Interference"
OFFENSIVE_HOLDING = "Offensive Holding"
DEFENSIVE_HOLDING = "Defensive Holding"

# What counts as an offensive snap for `plays_per_game` and the pace proxy.
# Kicks, punts, extra points and timeouts are excluded: the spec calls this
# "total offensive snaps", and a crew's share of special-teams plays is a
# property of the games' scorelines rather than of the crew.
OFFENSIVE_PLAY_TYPES = frozenset({"pass", "run"})

# A regulation game is 60 minutes of clock. `seconds_per_play` is that divided
# by offensive snaps — a pace proxy, and stated as one. It does not model
# overtime, which would lower a crew's figure for a reason that has nothing to
# do with the crew.
REGULATION_SECONDS = 3600.0


@dataclass(frozen=True)
class GamePenalties:
    """One game's penalty and pace counters, keyed by **modern** game id."""

    penalties: int = 0
    penalty_yards: float = 0.0
    dpi: int = 0
    dpi_yards: float = 0.0
    offensive_holding: int = 0
    defensive_holding: int = 0
    offensive_plays: int = 0


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


def _penalty_yards(raw: str) -> float:
    """The feed's `penalty_yards`, tolerating a blank or an `NA`.

    A play flagged with no enforced yardage (offsetting fouls, a foul enforced
    as a loss of down) carries an empty value here. Returning 0.0 rather than
    raising is right: the penalty itself is real and counted, and only its
    yardage is absent. Raising would drop the whole game over one row.
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def fetch_season_penalties(
    season: int,
    *,
    client: httpx.AsyncClient,
) -> dict[str, GamePenalties]:
    """One season's penalty counters, keyed by modern game id.

    Folded into counters **as the rows stream past**, so peak memory is one
    chunk plus one row plus 272 small counters — never the 93.4 MiB document.

    Raises on an upstream failure rather than returning an empty dict. That
    distinction carries real weight here: an empty dict is a legitimate answer
    only for a season whose games have all been played and produced no
    penalties, which never happens, so it must not be the way a 404 is
    reported.
    """
    url = source_ref(season)
    tallies: dict[str, Counter] = {}

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=url,
    ):
        if row["season_type"].strip().upper() != SEASON_TYPE:
            continue
        game_id = row["game_id"].strip()
        if not game_id:
            continue
        tally = tallies.setdefault(game_id, Counter())

        if row["play_type"].strip() in OFFENSIVE_PLAY_TYPES:
            tally["offensive_plays"] += 1

        if row["penalty"].strip() != "1":
            continue

        yards = _penalty_yards(row["penalty_yards"])
        tally["penalties"] += 1
        tally["penalty_yards"] += yards

        penalty_type = row["penalty_type"].strip()
        if penalty_type == DPI:
            tally["dpi"] += 1
            tally["dpi_yards"] += yards
        elif penalty_type == OFFENSIVE_HOLDING:
            tally["offensive_holding"] += 1
        elif penalty_type == DEFENSIVE_HOLDING:
            tally["defensive_holding"] += 1

    return {
        game_id: GamePenalties(
            penalties=tally["penalties"],
            penalty_yards=float(tally["penalty_yards"]),
            dpi=tally["dpi"],
            dpi_yards=float(tally["dpi_yards"]),
            offensive_holding=tally["offensive_holding"],
            defensive_holding=tally["defensive_holding"],
            offensive_plays=tally["offensive_plays"],
        )
        for game_id, tally in tallies.items()
    }

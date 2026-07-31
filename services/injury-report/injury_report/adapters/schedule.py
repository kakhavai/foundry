"""The schedule upstream — which clubs owe a report this week, and for which game.

This is the collector's **denominator**, and it is a separate upstream on
purpose. `coverage.expected` for `injury-report` is "one `team_injury_report`
per team with a scheduled game in the scoped week, per practice day elapsed",
and a denominator read out of the injury feed itself would be the exact
derivation `CoverageAccumulator`'s floor exists to defend against: a feed that
returns three teams would report three of three, ratio 1.0, while twenty-nine
clubs' reports silently vanished.

So the schedule answers *who must file* and `upstream.py` answers *who did*.
Only the disagreement between the two is interesting, and it is only visible
because they are two documents.

**Streamed and filtered as it is parsed.** The nflverse games file is every
game since 1999 and one week is roughly a fifth of a percent of it; see
`collector_core.streaming` and the roster-scope OOM write-up it carries.
"""

import os

import httpx
from collector_core.streaming import stream_csv_dicts

# Empty by default, which selects the deterministic stub below. Pointing this
# at the real feed is a ConfigMap edit, the same stub-mode reasoning
# `player-projections` applies to `PROJECTIONS_SNAPSHOT_URL`. The nflverse
# games file is the intended value and is what `weather` already reads:
#   https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv
SCHEDULE_URL_ENV = "SCHEDULE_URL"

# Schema-drift detection. An upstream that renames one of these must fail the
# capture loudly rather than map nulls into an append-only lake.
REQUIRED_COLUMNS = frozenset({"season", "week", "game_id", "home_team", "away_team"})

# Every club, in the order the stub schedule pairs them. Used only by the stub.
STUB_TEAMS: tuple[str, ...] = (
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


def schedule_url() -> str:
    """Read at call time, not import time, so a test or a redeploy can change
    it without reimporting the module."""
    return os.getenv(SCHEDULE_URL_ENV, "").strip()


def _stub_schedule(season: int, week: int) -> dict[str, str]:
    """A deterministic full slate: sixteen games, all thirty-two clubs.

    A *full* slate rather than a token two teams, so a stub deployment's
    coverage denominator is the real one and `collector_coverage_ratio` means
    something on day one. Deterministic so successive captures produce the same
    `game_id`s rather than a new universe each pass.
    """
    games: dict[str, str] = {}
    for index in range(0, len(STUB_TEAMS), 2):
        away, home = STUB_TEAMS[index], STUB_TEAMS[index + 1]
        game_id = f"{season}_{week:02d}_{away}_{home}"
        games[away] = game_id
        games[home] = game_id
    return games


async def fetch_scheduled_games(
    season: int, week: int, *, client: httpx.AsyncClient
) -> dict[str, str]:
    """`{team: game_id}` for every club with a game in the scoped week.

    Raises on an upstream failure rather than returning an empty mapping. An
    empty mapping would mean "no club owes a report", which reads as complete
    coverage of nothing — `capture.py` turns the exception into a `present: 0`
    envelope with a populated `errors` array instead.
    """
    url = schedule_url()
    if not url:
        return _stub_schedule(season, week)

    games: dict[str, str] = {}
    rows = stream_csv_dicts(client, url, required_columns=REQUIRED_COLUMNS)
    async for row in rows:
        # Filtered here, one row at a time, so the 6,000-odd games this
        # collector does not want are never materialized.
        if row["season"].strip() != str(season) or row["week"].strip() != str(week):
            continue
        game_id = row["game_id"].strip()
        for side in ("home_team", "away_team"):
            team = row[side].strip().upper()
            if team:
                games[team] = game_id
    return games

"""Schedule adapter — resolves the games in a scoped week.

Transitional by design. At 8B this is replaced by a call to the
`schedule-context` collector; the `ScheduledGame` interface stays put, so the
swap is a config change rather than a rewrite.

Two normalizations here are load-bearing and neither is obvious:

1. The feed's kickoff time is **Eastern, always** — never local to the venue.
   The season crosses the November DST transition, so a fixed offset is wrong
   for exactly the late-season games. Conversion goes through a real IANA zone.

2. For neutral-site games the feed's `stadium_id` and `roof` describe the
   DESIGNATED HOME TEAM's stadium, not where the game is played. Only the
   `stadium` name is correct. Trusting them fetches Detroit's weather for a
   game played in Munich — plausible numbers, passes every schema check, wrong
   by four thousand miles. Both fields are discarded for those rows.

**The response is streamed and filtered as it is parsed, never materialized.**
The feed is every game since 1999 — the scoped week is roughly a fifth of a
percent of it. `resp.text` handed to `io.StringIO` held the document three
times over (httpx's buffered bytes, the decoded `str`, and the `StringIO`
copy) to keep sixteen rows. That is the shape that OOM-killed `roster-scope`
at a 256Mi limit, at a size where it happened not to matter here; `weather` is
the collector twenty-four more will be copied from, so it does not model the
pattern that failed. See `collector_core.streaming`.
"""

import csv
import io
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from collector_core.streaming import stream_csv_dicts

SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly rather than map nulls into
# an append-only lake. These are the columns the mapping below actually reads.
REQUIRED_COLUMNS = frozenset(
    {
        "season",
        "week",
        "game_id",
        "gameday",
        "gametime",
        "home_team",
        "away_team",
        "location",
        "stadium",
    }
)

# The feed publishes kickoff in this zone regardless of where the game is played.
_FEED_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ScheduledGame:
    game_id: str
    season: int
    week: int
    kickoff_at: datetime
    home_team: str
    away_team: str
    stadium_id: str | None
    stadium_name: str
    is_neutral_site: bool
    roof_raw: str | None


def _kickoff_utc(gameday: str, gametime: str, game_id: str) -> datetime:
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(
            f"unparseable kickoff for {game_id}: {gameday!r} {gametime!r}"
        ) from exc
    return naive.replace(tzinfo=_FEED_TIMEZONE).astimezone(tz=ZoneInfo("UTC"))


def _in_scope(row: dict, season: int, week: int) -> bool:
    return row["season"] == str(season) and row["week"] == str(week)


def _to_game(row: dict, season: int, week: int) -> ScheduledGame:
    is_neutral = row["location"].strip().lower() == "neutral"
    roof = (row.get("roof") or "").strip() or None
    stadium_id = (row.get("stadium_id") or "").strip() or None
    return ScheduledGame(
        game_id=row["game_id"],
        season=season,
        week=week,
        kickoff_at=_kickoff_utc(row["gameday"], row["gametime"], row["game_id"]),
        home_team=row["home_team"],
        away_team=row["away_team"],
        # Discarded for neutral sites — see the module docstring.
        stadium_id=None if is_neutral else stadium_id,
        stadium_name=row["stadium"],
        is_neutral_site=is_neutral,
        roof_raw=None if is_neutral else roof,
    )


def parse_schedule_csv(text: str, *, season: int, week: int) -> list[ScheduledGame]:
    """Parse a whole document. Convenience for tests and small inputs — the
    production path streams instead (`fetch_schedule`)."""
    return [
        _to_game(row, season, week)
        for row in csv.DictReader(io.StringIO(text))
        if _in_scope(row, season, week)
    ]


async def fetch_schedule(
    season: int, week: int, client: httpx.AsyncClient
) -> list[ScheduledGame]:
    """Stream the feed, keeping only the scoped week.

    The out-of-scope rows — every game since 1999 — are discarded as they go
    past rather than materialized and filtered afterwards.
    """
    return [
        _to_game(row, season, week)
        async for row in stream_csv_dicts(
            client, SCHEDULE_URL, required_columns=REQUIRED_COLUMNS
        )
        if _in_scope(row, season, week)
    ]

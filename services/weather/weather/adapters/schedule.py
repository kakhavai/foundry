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
"""

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

SCHEDULE_URL = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
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


def parse_schedule_csv(text: str, *, season: int, week: int) -> list[ScheduledGame]:
    games: list[ScheduledGame] = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["season"] != str(season) or row["week"] != str(week):
            continue

        is_neutral = row["location"].strip().lower() == "neutral"
        roof = (row.get("roof") or "").strip() or None
        stadium_id = (row.get("stadium_id") or "").strip() or None

        games.append(
            ScheduledGame(
                game_id=row["game_id"],
                season=season,
                week=week,
                kickoff_at=_kickoff_utc(
                    row["gameday"], row["gametime"], row["game_id"]
                ),
                home_team=row["home_team"],
                away_team=row["away_team"],
                # Discarded for neutral sites — see the module docstring.
                stadium_id=None if is_neutral else stadium_id,
                stadium_name=row["stadium"],
                is_neutral_site=is_neutral,
                roof_raw=None if is_neutral else roof,
            )
        )
    return games


async def fetch_schedule(
    season: int, week: int, client: httpx.AsyncClient
) -> list[ScheduledGame]:
    resp = await client.get(SCHEDULE_URL)
    resp.raise_for_status()
    return parse_schedule_csv(resp.text, season=season, week=week)

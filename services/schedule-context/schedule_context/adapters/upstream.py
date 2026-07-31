"""The upstream adapter — the only module that knows the wire format.

The feed is the nflverse game table: every game since 1999 in one CSV,
~2.1 MB and growing by a season a year. A capture keeps one season of it
(~285 rows), which is roughly four percent.

**It is streamed and filtered as it is parsed, never materialised.**
`response.text` handed to `io.StringIO` holds the document three times over
(httpx's buffered bytes, the decoded `str`, the `StringIO` copy) — the exact
shape that OOM-killed `roster-scope` at a 256Mi limit. `weather` reads this
same feed and was rewritten to stream; this collector does not reintroduce
the pattern its neighbour just removed. See `collector_core.streaming`.

**A whole season, not a week, and that is not an optimisation to remove.**
Rest, bye adjacency, road stretch and acclimatisation are path-dependent:
what happened to a team in week 6 is a fact about weeks 1 through 5. Fetching
only the scoped week would silently produce `days_rest: null` for every team.

**The feed's own `away_rest` / `home_rest` columns are deliberately ignored.**
They are whole-day integers derived from calendar dates, which is the precise
failure this collector's spec names: a Sunday 13:00 game followed by a
Thursday 20:20 game is 3.3 days of rest, and date subtraction reports 4 —
attenuating every short-week effect by exactly the amount that matters.
`days_rest` here is computed from kickoff timestamps.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from collector_core.streaming import stream_csv_dicts

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "nflverse-games"

# Environment-overridable so a load test or a fixture server can stand in for
# the real feed without hammering a third party — the same reasoning that put
# `FORECAST_URL` and `SCHEDULE_URL` behind env vars during 8A.
UPSTREAM_URL = os.getenv(
    "SCHEDULE_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly with `reason=malformed`
# rather than map nulls into an append-only lake. These are exactly the columns
# the mapping below reads — no more, so an unrelated column disappearing
# upstream does not fail a capture that never used it.
REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "season",
        "game_type",
        "week",
        "gameday",
        "gametime",
        "away_team",
        "home_team",
        "location",
        "stadium",
    }
)

# The feed publishes kickoff in this zone regardless of where the game is
# played — a London game at 14:30 local appears as 09:30. The season crosses
# the November DST transition, so a fixed -05:00 is wrong for exactly the
# late-season games; conversion goes through a real IANA zone.
FEED_TIMEZONE = ZoneInfo("America/New_York")

# Preseason games are excluded: they are not part of the competitive schedule,
# starters barely play, and counting them would insert phantom rest gaps into
# every team's week-1 chain.
EXCLUDED_GAME_TYPES = frozenset({"PRE"})


@dataclass(frozen=True)
class ScheduledGame:
    """One row of the feed, normalised. `kickoff_at` is UTC and aware.

    `kickoff_at` is `None` for a game the feed lists without a usable kickoff
    time — a scheduled-but-unslotted late-season game, or a postponement whose
    replacement time has not been published. That is a real upstream ambiguity
    and it is represented rather than guessed: the capture counts the row as
    expected-and-missing with a reason instead of inventing a kickoff, which
    would corrupt the rest chain of both teams for the rest of the season.
    """

    game_id: str
    season: int
    week: int
    game_type: str
    kickoff_at: datetime | None
    home_team: str
    away_team: str
    is_neutral_site: bool
    stadium_name: str


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL or None


def parse_kickoff(gameday: str, gametime: str) -> datetime | None:
    """`2026-09-13` + `13:00` in the feed's zone -> an aware UTC instant.

    Returns `None` for an absent or unparseable time rather than raising: one
    unslotted game must not fail a whole season's capture, and the caller
    already has a place to record it.
    """
    gameday, gametime = gameday.strip(), gametime.strip()
    if not gameday or not gametime:
        return None
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    # `replace(tzinfo=FEED_TIMEZONE)`, because the feed's wall clock IS
    # Eastern and zoneinfo resolves the right DST offset for that date.
    # `replace(tzinfo=UTC)` would silently move every kickoff by four hours,
    # and nothing downstream could tell.
    return naive.replace(tzinfo=FEED_TIMEZONE).astimezone(UTC)


def _to_game(row: dict) -> ScheduledGame:
    return ScheduledGame(
        game_id=row["game_id"],
        season=int(row["season"]),
        week=int(row["week"]),
        game_type=row["game_type"].strip().upper(),
        kickoff_at=parse_kickoff(row["gameday"], row["gametime"]),
        home_team=row["home_team"].strip(),
        away_team=row["away_team"].strip(),
        is_neutral_site=row["location"].strip().lower() == "neutral",
        stadium_name=row["stadium"].strip(),
    )


def _in_scope(row: dict, season: int) -> bool:
    """Kept or discarded as the row goes past — never materialised first."""
    if row["season"] != str(season):
        return False
    return row["game_type"].strip().upper() not in EXCLUDED_GAME_TYPES


async def fetch_season_games(
    season: int,
    *,
    client: httpx.AsyncClient,
) -> list[ScheduledGame]:
    """Every competitive game of one season, in feed order.

    Raises on an upstream failure rather than returning an empty list, so the
    caller can turn the exception into a `present: 0` envelope. An empty list
    would instead be recorded as a successful capture of nothing.
    """
    return [
        _to_game(row)
        async for row in stream_csv_dicts(
            client, UPSTREAM_URL, required_columns=REQUIRED_COLUMNS
        )
        if _in_scope(row, season)
    ]

"""The schedule feed — read here only for the crosswalk it publishes.

`schedule-context` owns `game_id`. This collector's other two upstreams do not
speak it:

    officials.csv   game_id = 2025090400        the legacy gamekey
    play_by_play    game_id = 2025_01_DAL_PHI   the modern id

The spec says the join between crew data and penalty data "is by
hand-maintained crosswalk". **It is not, and building one would have been a
mistake.** `games.csv` carries `old_game_id` holding exactly the legacy value
alongside the modern `game_id`, so the crosswalk is published, versioned and
maintained by somebody else. Measured live before it was relied on: all 272
rows of the 2025 regular season join, none unmatched in either direction.

The feed also carries a `referee` column, which is a free cross-check on the
crew join — see `officiating.crews.referee_disagreement`. It is a check, never
a source: the names are *display* forms and one of them ("Ron Torbert" here,
"Ronald Torbert" in officials.csv) disagrees on all 17 of that crew's games.
Resolving crews by `official_id` is what makes that a footnote instead of a
missing crew.

Streamed and filtered as it is parsed, never materialised — the feed is ~2.1 MB
of every game since 1999 and a capture keeps one season of it. `columns=` narrows
each row to the seven fields below rather than all 46.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-games"

# Environment-overridable so a load test or a fixture server can stand in for
# the real feed without hammering a third party — the same reasoning that put
# `SCHEDULE_URL` behind an env var in `schedule-context`, and the same default
# value, because it is the same document.
UPSTREAM_URL = os.getenv(
    "SCHEDULE_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)

# Exactly the columns read below — no more, so an unrelated column disappearing
# upstream does not fail a capture that never used it. Asserted against the
# header before a row is yielded, per the Phase 8 failure-handling contract: a
# renamed field fails loudly rather than mapping nulls into an append-only lake.
REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "old_game_id",
        "season",
        "game_type",
        "week",
        "gameday",
        "referee",
    }
)

# Regular season only, and that is a modelling decision rather than a filter of
# convenience. Postseason crews are assembled on merit from across the regular
# season's crews — the spec names "postseason reshuffle" itself — so a January
# game's seven officials are mostly not the crew whose season it would extend.
# Folding them into a crew's rate window would attribute a reshuffled group's
# penalties to a crew that no longer exists.
SEASON_GAME_TYPE = "REG"


@dataclass(frozen=True)
class ScheduledGame:
    """One regular-season game, carrying both ids so the join can be made.

    `referee` is the feed's display name for the white hat, kept only for the
    cross-check. Nothing downstream reads it as identity.
    """

    game_id: str
    legacy_game_id: str
    season: int
    week: int
    gameday: str
    referee: str


def source_ref() -> str:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL


def _in_scope(row: dict, season: int) -> bool:
    """Kept or discarded as the row goes past — never materialised first."""
    if row["season"] != str(season):
        return False
    if row["game_type"].strip().upper() != SEASON_GAME_TYPE:
        return False
    # A row with no `old_game_id` cannot be joined to officials.csv at all, and
    # a blank one would collide with every other blank in the crosswalk dict —
    # silently mapping many games onto one key.
    return bool(row["old_game_id"].strip())


def _to_game(row: dict) -> ScheduledGame:
    return ScheduledGame(
        game_id=row["game_id"].strip(),
        legacy_game_id=row["old_game_id"].strip(),
        season=int(row["season"]),
        week=int(row["week"]),
        gameday=row["gameday"].strip(),
        referee=row["referee"].strip(),
    )


async def fetch_season_games(
    season: int,
    *,
    client: httpx.AsyncClient,
) -> list[ScheduledGame]:
    """Every regular-season game of one season, in feed order.

    Raises on an upstream failure rather than returning an empty list, so the
    caller can turn the exception into a `present: 0` envelope. An empty list
    would instead be recorded as a successful capture of nothing.

    `etag_key` is the URL — the same string the envelope records as
    `upstream.source_ref`, so the cache key and the provenance cannot drift. A
    `304` raises `UpstreamUnchanged` before a row is yielded; the caller
    re-raises it above its generic handler.
    """
    return [
        _to_game(row)
        async for row in stream_csv_dicts(
            client,
            UPSTREAM_URL,
            required_columns=REQUIRED_COLUMNS,
            columns=REQUIRED_COLUMNS,
            etag_key=UPSTREAM_URL,
        )
        if _in_scope(row, season)
    ]

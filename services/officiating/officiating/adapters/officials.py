"""The officials feed — who actually worked each game.

nflverse's `officials.csv`: 1.29 MB, 21,900 rows, seasons 2015-2025, one row
per official per game. Columns: `game_id` (the legacy gamekey), `game_key`,
`official_name`, `position`, `jersey_number`, `official_id`, `season`,
`season_type`, `week`.

**`official_id` is present, and it is the reason this collector can exist.**
The spec's adapter note — "an adapter must resolve individual officials, not
just the referee's name, because crews are reassembled between seasons and
substituted within them" — is satisfiable rather than aspirational. Every 2025
row carries one; measured, not assumed.

**This feed is post-game.** It records who worked a game that has already been
played, not who is scheduled to work one. Two consequences run through the
whole collector:

1. `assignment_announced_at` and `is_provisional` have no free source and are
   emitted as `null` with a recorded reason. See `officiating.crews`.
2. "whose crew has been published", in the spec's coverage rule, effectively
   means "which has been played". That is a narrower universe than the season,
   and it must **not** be allowed to become the expectation — `EXPECTED_FLOOR`
   stays at the 272 games the season is known to have. A feed that returns
   half the season is a coverage miss, not a smaller season.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-officials"

UPSTREAM_URL = os.getenv(
    "OFFICIALS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/officials/officials.csv",
)

REQUIRED_COLUMNS = frozenset(
    {
        "game_id",
        "official_name",
        "position",
        "official_id",
        "season",
        "season_type",
    }
)

SEASON_TYPE = "REG"

# The seven on-field officials of a modern crew. Alternates and the replay
# official are excluded deliberately, and the exclusion is load-bearing rather
# than tidy: they appear irregularly (10 of 272 games in 2025 list 12 people,
# 3 list 14), they do not throw flags, and counting them would make
# `crew_continuity_pct` read as churn on exactly the games where an alternate
# happened to be recorded. With them excluded, all 272 games of the 2025 regular
# season carry exactly seven — measured.
#
# "Head Linesman" was renamed "Down Judge" in 2017 and both spellings are still
# in the historical file, so both are listed.
ON_FIELD_POSITIONS = frozenset(
    {
        "Referee",
        "Umpire",
        "Down Judge",
        "Head Linesman",
        "Line Judge",
        "Field Judge",
        "Side Judge",
        "Back Judge",
    }
)

# The white hat. The crew's public identity, and the key this collector derives
# `crew_id` from — by `official_id`, never by this string.
REFEREE_POSITION = "Referee"

CREW_SIZE = 7


@dataclass(frozen=True)
class Official:
    """One official's appearance in one game."""

    official_id: str
    name: str
    position: str


def source_ref() -> str:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL


def _in_scope(row: dict, season: int) -> bool:
    """Kept or discarded as the row goes past — never materialised first.

    A row with no `official_id` is dropped here rather than downstream: an
    official with no id cannot be tracked across games, so counting them
    towards a crew's continuity would credit stability this collector cannot
    actually observe. It is also the one field with no fallback — a name is a
    display form, as `games.csv`'s `referee` column demonstrates.
    """
    if row["season"] != str(season):
        return False
    if row["season_type"].strip().upper() != SEASON_TYPE:
        return False
    if row["position"].strip() not in ON_FIELD_POSITIONS:
        return False
    return bool(row["official_id"].strip())


async def fetch_season_officials(
    season: int,
    *,
    client: httpx.AsyncClient,
) -> dict[str, list[Official]]:
    """One season's on-field officials, keyed by **legacy** game id.

    Legacy, because that is what this feed speaks; `officiating.adapters.games`
    supplies the crosswalk to the modern id. Translating here would put the
    join inside the adapter that cannot see the schedule.

    Raises on an upstream failure rather than returning an empty dict, so the
    caller can turn the exception into a `present: 0` envelope.
    """
    crews: dict[str, list[Official]] = {}
    async for row in stream_csv_dicts(
        client,
        UPSTREAM_URL,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        etag_key=UPSTREAM_URL,
    ):
        if not _in_scope(row, season):
            continue
        crews.setdefault(row["game_id"].strip(), []).append(
            Official(
                official_id=row["official_id"].strip(),
                name=row["official_name"].strip(),
                position=row["position"].strip(),
            )
        )
    return crews

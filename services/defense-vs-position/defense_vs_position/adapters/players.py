"""The league's roster positions: `gsis_id -> position`, and nothing else.

Play-by-play names the player who caught, ran or kicked, and never says what
position he plays. Something has to supply that, and the two candidates were
`pbp_participation_<season>.csv` (46.82 MiB) and `players.csv.gz` (2.39 MiB).

`players.csv.gz` wins on field-per-megabyte by a factor of twenty, because the
two feeds carry the **same fact**: participation's `offense_positions` is the
roster-listed position of each player on the field, which is the same string
`players.csv` holds once per player, repeated once per snap. See `pbp.py` for
the full comparison and for why participation's `route` column cannot supply
`opportunities_defended` either.

Freshness-checked before format, per `docs/collectors.md` -- the rule
`player-contract` earned. On 2026-08-01 the `players` release stamped
`players.csv` , `players.csv.gz`, `players.parquet` and `players.rds` all at
`2026-08-01T09:55`, so no format is abandoned and the ordinary size rule
applies: take the `.csv.gz`.

**Filtered as it parses**, to the fantasy-eligible positions only. 25,036
players carry a GSIS id and the collector can attribute an opportunity to
about a third of them; the rest are never built into a dict.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

from ..scoring import ROSTER_TO_FANTASY

UPSTREAM_ADAPTER = "nflverse-players"

UPSTREAM_URL = os.getenv(
    "PLAYERS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/players/"
    "players.csv.gz",
)

REQUIRED_COLUMNS = frozenset(
    {
        "gsis_id",
        "display_name",
        "position",
        "jersey_number",
        "latest_team",
    }
)


@dataclass(frozen=True)
class PlayerRef:
    """One player's roster facts: their fantasy position, and enough to ask
    `player-identity` who they are."""

    gsis_id: str
    # The nflverse roster code, as published (`QB`, `FB`, `K`, ...). Sent to
    # `player-identity` verbatim; see `identity.py` for why that is checked.
    roster_position: str
    # The spec's `position` enum member this is scored as. `FB` becomes `RB`.
    fantasy_position: str
    name: str
    team: str | None
    jersey_number: int | None


def source_ref() -> str:
    return UPSTREAM_URL


async def fetch_positions(
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> dict[str, PlayerRef]:
    """`gsis_id -> PlayerRef`, for fantasy-eligible positions only.

    Raises on an upstream failure. An empty or partial map would silently
    delete opportunities from every defense's row rather than fail the pass,
    which is exactly the plausible-looking wrong answer the coverage floor
    cannot see: the rows would all still be there, just smaller.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    positions: dict[str, PlayerRef] = {}

    async for row in stream_csv_dicts(
        client,
        UPSTREAM_URL,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=UPSTREAM_URL,
        **kwargs,
    ):
        gsis_id = row["gsis_id"].strip()
        if not gsis_id:
            continue
        roster_position = row["position"].strip().upper()
        fantasy_position = ROSTER_TO_FANTASY.get(roster_position)
        if fantasy_position is None:
            continue
        jersey_raw = row["jersey_number"].strip()
        team = row["latest_team"].strip()
        positions[gsis_id] = PlayerRef(
            gsis_id=gsis_id,
            roster_position=roster_position,
            fantasy_position=fantasy_position,
            name=row["display_name"].strip(),
            team=team or None,
            jersey_number=int(float(jersey_raw)) if _is_number(jersey_raw) else None,
        )

    return positions


def _is_number(raw: str) -> bool:
    try:
        float(raw)
    except ValueError:
        return False
    return True

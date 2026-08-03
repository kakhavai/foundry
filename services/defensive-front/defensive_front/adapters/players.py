"""The roster map: who is a front defender, and what to tell `player-identity`.

Two uses, both narrow:

* **Front membership.** `front_continuity_index` counts snaps by front
  players, so the eleven ids on each charted snap have to be split from the
  secondary. That is `position_group` — `DL` and `LB` against `DB` — and it is
  **not** the interior/edge split the spec rules out. The spec's objection is
  specific and does not generalise: "every 3-4 outside linebacker lands in the
  wrong bucket" is a statement about splitting the front, and a 3-4 outside
  linebacker is listed `OLB`, which is in `LB`, which is in the front either
  way. No listed position puts a safety in the front or a nose tackle out of
  it, so this coarse cut has no failure mode of that kind. The fine cut has
  one, and is therefore not made — see `ratings.UNITS`.
* **Resolve queries.** `key_absences` publishes canonical `fdy-` ids, so the
  injury feed's GSIS ids are resolved through `player-identity`, which scores
  `team`, `position` and `jersey_number` server-side.

Format: `players.csv.gz`, 2.39 MiB. Re-checked live for this build —
`players.csv`, `.csv.gz`, `.parquet` and `.rds` all carry
`2026-08-01T09:55:1{7,8}Z`, so the freshness exception does not apply. (Only
`players.qs`, an R serialisation this fleet never reads, lags at
2026-03-19.) The gzipped CSV is the fleet rule.

Filtered as it parses: 25,036 rows in, 6,984 front players kept. Materialising
the other 18,052 first is the waste rule 2 of the memory rules exists for.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-players"

UPSTREAM_URL = os.getenv(
    "PLAYERS_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv.gz",
)

REQUIRED_COLUMNS = frozenset(
    {
        "gsis_id",
        "position",
        "position_group",
        "latest_team",
        "jersey_number",
    }
)

# The two `position_group` values that make up a defensive front. See the
# module docstring for why this coarse cut is sound where the interior/edge
# one is not.
FRONT_POSITION_GROUPS = frozenset({"DL", "LB"})


@dataclass(frozen=True, slots=True)
class PlayerRef:
    """One front defender, as much as `player-identity` is told about him."""

    gsis_id: str
    position: str
    team: str
    jersey_number: int | None


def source_ref() -> str:
    """The exact upstream artifact this pass read. Also the ETag cache key."""
    return UPSTREAM_URL


async def fetch_front_players(
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> dict[str, PlayerRef]:
    """`gsis_id -> PlayerRef` for every front defender in the roster feed.

    Raises on an upstream failure. `capture` degrades two fields rather than
    failing the pass: without this map there is no front membership, so
    `front_continuity_index` and `key_absences` are null with
    `players_unavailable` recorded on the row.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    players: dict[str, PlayerRef] = {}

    async for row in stream_csv_dicts(
        client,
        UPSTREAM_URL,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=UPSTREAM_URL,
        **kwargs,
    ):
        if row["position_group"].strip() not in FRONT_POSITION_GROUPS:
            continue
        gsis_id = row["gsis_id"].strip()
        if not gsis_id:
            continue
        jersey_raw = row["jersey_number"].strip()
        jersey: int | None
        try:
            jersey = int(float(jersey_raw)) if jersey_raw else None
        except ValueError:
            jersey = None
        players[gsis_id] = PlayerRef(
            gsis_id=gsis_id,
            position=row["position"].strip(),
            team=row["latest_team"].strip(),
            jersey_number=jersey,
        )

    return players

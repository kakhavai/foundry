"""The roster feed: the `pfr_id` -> `gsis_id` crosswalk, and IR status.

Two uses, both narrow, and the first one is structural rather than decorative.

**The crosswalk.** `snap_counts` is keyed by `pfr_player_id` and
`depth_charts` by `gsis_id`, and nothing joins them directly. This feed is the
only free document carrying both, so it is what turns "who took the snaps"
into "who the depth chart calls the left tackle". Measured live on the 2025
regular season: 22,555 usable `pfr_id`/`gsis_id` pairs, covering **4,195 of
4,212** offensive-line snap rows (99.6%).

**IR.** `starter_availability`'s enum includes `ir`, and the weekly injury
report cannot produce it — its `report_status` column carries exactly `Out`,
`Questionable`, `Doubtful` and blank (verified: 6,068 rows on 2025, no other
value). `ir` is a **roster** designation, and this feed's `status` column is
where it lives: `RES` (reserve/injured), 623 of 4,125 offensive linemen. That
is the same distinction `defensive-front` drew in the other direction — it
needed game status and refused to read roster status for it — applied to the
one field that genuinely is roster status. Since the spec's failure mode is
literally "when a starter goes to IR mid-week", sourcing `ir` from the feed
that publishes it is not optional polish.

`latest_team` is deliberately **not** read. It is a career-latest field and
names the wrong club for anyone traded mid-season; the team on every row this
collector builds comes from `snap_counts`, which is per game.

--------------------------------------------------------------------------
Format
--------------------------------------------------------------------------

`players.csv.gz`, 2.39 MiB. `csv`, `csv.gz`, `parquet` and `rds` share a
timestamp so the abandoned-artifact exception does not apply and the fleet
rule takes the gzipped CSV. 25,036 rows in, filtered as they are parsed.
"""

import os
from dataclasses import dataclass, field

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
        "pfr_id",
        "position",
        "position_group",
        "jersey_number",
        "status",
    }
)

# The `position_group` that makes up an offensive line. One value, and no
# finer cut is attempted: the sidedness the spec's enum needs comes from
# `depth_charts`, not from a listed position.
LINE_POSITION_GROUP = "OL"

# `status` codes that mean the player is on injured reserve. `RES` is
# reserve/injured; `PUP` is physically-unable-to-perform, which is a distinct
# list but has the same consequence for a projection — the man is not playing.
# Both map to the spec's `ir`, and nothing else does.
IR_STATUSES = frozenset({"RES", "PUP"})


@dataclass(frozen=True, slots=True)
class PlayerRef:
    """One offensive lineman, as much as `player-identity` is told about him."""

    gsis_id: str
    pfr_id: str
    position: str
    jersey_number: int | None
    on_ir: bool


@dataclass
class Roster:
    """The crosswalk and the reference map, both keyed for their own caller."""

    by_gsis: dict[str, PlayerRef] = field(default_factory=dict)
    gsis_for_pfr: dict[str, str] = field(default_factory=dict)


def source_ref() -> str:
    """The exact upstream artifact this pass read. Also the ETag cache key."""
    return UPSTREAM_URL


async def fetch_line_players(
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> Roster:
    """Every offensive lineman in the roster feed, both ways round.

    Raises on an upstream failure. `capture` degrades the starter half rather
    than failing the pass: without the crosswalk no snap row can be named, so
    there is no lineup hash and no starter row, while every unit rate is
    unaffected.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    roster = Roster()

    async for row in stream_csv_dicts(
        client,
        UPSTREAM_URL,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=UPSTREAM_URL,
        **kwargs,
    ):
        if row["position_group"].strip().upper() != LINE_POSITION_GROUP:
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

        pfr_id = row["pfr_id"].strip()
        reference = PlayerRef(
            gsis_id=gsis_id,
            pfr_id=pfr_id,
            position=row["position"].strip().upper(),
            jersey_number=jersey,
            on_ir=row["status"].strip().upper() in IR_STATUSES,
        )
        roster.by_gsis[gsis_id] = reference
        if pfr_id:
            roster.gsis_for_pfr[pfr_id] = gsis_id

    return roster

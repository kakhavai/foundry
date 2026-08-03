"""The weekly injury report — game status for the upcoming week.

`starter_availability` is "`active`, `questionable`, `doubtful`, `out`, `ir`
for the **upcoming week**", which is game status rather than roster status.
`players.csv`'s `status` column is roster status and says nothing about
whether a healthy-rostered starter is sitting Sunday; reading it as this field
would populate a different quantity under the same name. `defensive-front`
added this feed for exactly the same reason and the reasoning transfers
unchanged.

`ir` is the one value this feed **cannot** produce, and that is a fact about
the document rather than a gap in the adapter. Verified on the real 2025
season, 6,068 report rows: `report_status` carries `Out` (1,396),
`Questionable` (1,281), `Doubtful` (106) and blank (3,285), and no other
value at all. So `ir` comes from `players.csv`'s `RES`/`PUP` roster status —
see `players.py` — and `capture` merges the two, roster status winning,
because a man on injured reserve is not merely doubtful.

A blank `report_status` is a practice-report-only entry and maps to `active`:
the player is on the report for a limited practice and is expected to play.
That is the same reading `defensive-front` gives it, where a blank is
deliberately not an absence.

--------------------------------------------------------------------------
Format
--------------------------------------------------------------------------

`injuries_<season>.csv.gz`, 0.12 MiB against a 0.09 MiB parquet and a 0.66 MiB
plain CSV, all sharing a timestamp — the fleet rule takes the gzipped CSV.
0.15% of a ~78 MiB pass.
"""

import os

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-injuries"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "INJURIES_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/injuries/"
    "injuries_{season}.csv.gz",
)

REQUIRED_COLUMNS = frozenset(
    {
        "season",
        "game_type",
        "team",
        "week",
        "gsis_id",
        "position",
        "report_status",
    }
)

SEASON_TYPE = "REG"

AVAILABILITY_ACTIVE = "active"
AVAILABILITY_IR = "ir"

# The feed's own wording, case-folded, mapped onto the spec's enum. Anything
# unrecognised falls through to `active` rather than being invented into a new
# enum value — the schema restricts the enum, so a fabricated status would
# fail conformance rather than reach the lake, which is the intent.
REPORT_STATUS = {
    "out": "out",
    "doubtful": "doubtful",
    "questionable": "questionable",
    "": AVAILABILITY_ACTIVE,
}


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read. Also the ETag cache key."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_availability(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> dict[str, str]:
    """`gsis_id -> availability` for the week **after** `week`.

    `week` is the last week this pass sampled, so the upcoming week is
    `week + 1` — the week the spec asks about and the one a generator
    projecting the next slate needs. A pass run at the end of a season finds
    no such week and returns an empty map, which is correct: there is no
    upcoming game to be available for, and every starter then reads `active`
    by default rather than being falsely marked out.

    Not narrowed to linemen here: that needs the roster map, which is a
    different feed. `capture` intersects the two.

    Raises on an upstream failure. `capture` degrades one field rather than
    failing the pass.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    upcoming = week + 1
    availability: dict[str, str] = {}

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=url,
        **kwargs,
    ):
        if row["game_type"].strip().upper() != SEASON_TYPE:
            continue
        week_raw = row["week"].strip()
        if not week_raw.isdigit() or int(week_raw) != upcoming:
            continue
        gsis_id = row["gsis_id"].strip()
        if not gsis_id:
            continue
        status = REPORT_STATUS.get(
            row["report_status"].strip().lower(), AVAILABILITY_ACTIVE
        )
        availability[gsis_id] = status

    return availability

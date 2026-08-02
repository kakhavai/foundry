"""The weekly injury report — the only free source of "out or doubtful".

**A fourth feed, added deliberately.** The spec's `key_absences` is "front
starters listed out or doubtful for the **upcoming week**", which is game
status, not roster status. `players.csv` publishes `status` (`ACT`, `RES`,
`DEV`, ...) — a roster fact that says nothing about whether a healthy-rostered
starter is sitting Sunday, and reading it as an absence would populate the
field with a different quantity than the one the spec names.

The cost of being correct here is 0.121 MiB of a ~67 MiB pass — 0.18%. Sizes
re-checked live for this build: `injuries_2025.csv.gz` 0.121 MiB against a
0.093 MiB parquet and a 0.663 MiB plain CSV, all timestamped
`2026-03-18T12:45:3{1,2}Z`, so the freshness exception does not apply and the
fleet rule takes the `.csv.gz`.

Measured on the real 2025 season: 6,068 report rows, of which 1,396 are `Out`
and 106 `Doubtful`. `Questionable` (1,281) is deliberately **not** an absence —
the spec says out or doubtful, and questionable players overwhelmingly play.
A blank `report_status` (3,285 rows) is a practice-report-only entry and is
likewise not an absence.
"""

import os
from dataclasses import dataclass

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

# The spec's two words, exactly. Case-folded because the feed writes them
# title-cased and a schema change to upper case must not silently empty the
# field.
ABSENT_STATUSES = frozenset({"out", "doubtful"})

SEASON_TYPE = "REG"


@dataclass(frozen=True, slots=True)
class Absence:
    """One player listed out or doubtful, before identity resolution."""

    gsis_id: str
    team: str
    position: str
    status: str


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read. Also the ETag cache key."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_absences(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> list[Absence]:
    """Everyone out or doubtful for the week **after** `week`.

    `week` is the last week this pass sampled, so the upcoming week is
    `week + 1` — which is the week the spec asks about and the one a generator
    projecting the next slate needs. A pass run at the end of a season finds
    no such week and returns an empty list, which is correct: there is no
    upcoming game to be absent from.

    Not narrowed to front players here: that needs the roster map, which is a
    different feed. `capture` intersects the two.

    Raises on an upstream failure. `capture` degrades one field rather than
    failing the pass.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    upcoming = week + 1
    absences: list[Absence] = []

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
        if row["report_status"].strip().lower() not in ABSENT_STATUSES:
            continue
        gsis_id = row["gsis_id"].strip()
        team = row["team"].strip()
        if not gsis_id or not team:
            continue
        absences.append(
            Absence(
                gsis_id=gsis_id,
                team=team,
                position=row["position"].strip(),
                status=row["report_status"].strip(),
            )
        )

    return absences

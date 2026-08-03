"""Participation charting: pressure conceded and release timing.

The only module that knows this feed's wire format. It carries three of this
collector's sourceable unit fields — `pressure_rate_allowed`, its adjusted
counterpart and `mean_time_to_throw` — plus the pressure half of the opponent
yardstick, so its loss ends the pass rather than nulling a column. See
`capture`.

--------------------------------------------------------------------------
Format, and why this is the one feed that is not gzipped
--------------------------------------------------------------------------

| asset | updated | size |
|---|---|---|
| `pbp_participation_2025.csv` | 2026-02-10T18:54:08Z | 46.82 MiB |
| `pbp_participation_2025.parquet` | 2026-02-10T18:54:07Z | 4.52 MiB |
| *(no `.csv.gz` is published)* | | |

Same timestamp, so the freshness exception does not apply. `docs/collectors.md`
allows revisiting the no-parquet rule only for an asset that (a) has no `.gz`
variant, (b) exceeds ~40 MiB **and** (c) ships `CAPTURE_ENABLED=true`. This
feed meets (a) and (b) and fails (c): the loop is off, so the 42 MiB the
parquet would save is spent only on a hand-dispatched `POST /refresh`, against
a `pyarrow` wheel that is 47.8 MiB in the image (>100 MiB installed) for
**every** collector. Plain CSV with `columns=`.

Column projection is not optional here. The feed carries eight
semicolon-joined 22-player lists per row and this adapter reads **none** of
them — see below — so the projection is most of the document's bulk.

--------------------------------------------------------------------------
Pressure is attributed to the UNIT, and the spec asks to be told
--------------------------------------------------------------------------

> The adapter must attribute pressures to a specific blocker where the
> upstream supports it and to the unit where it does not, and must say which
> in `upstream.adapter`.

It does not support it. `was_pressure` is charted at the **play**, with no
blocker column and nothing that names which of the five conceded it;
`offense_players` is an unordered list of the eleven men on the field, which
would let a collector *distribute* a pressure across the line and call it
attribution. That would publish five populated, plausible, invented numbers.

So attribution is unit-level, `offense_players` is not even read, and
`capture.UPSTREAM_ADAPTER` says `unit-attributed` in every envelope. The
spec's requirement is met by disclosure rather than by synthesis.

--------------------------------------------------------------------------
Populated rates, measured live on the real 2025 season
--------------------------------------------------------------------------

Of 22,002 charted pass-rush snaps: `was_pressure` 100%,
`number_of_pass_rushers` 100%, `time_to_throw` 42.8%. The last being under
half is correct rather than a gap — a sack, a scramble and a throwaway have no
release.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-pbp-participation"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "PARTICIPATION_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/pbp_participation/"
    "pbp_participation_{season}.csv",
)

REQUIRED_COLUMNS = frozenset(
    {
        "nflverse_game_id",
        "play_id",
        "number_of_pass_rushers",
        "was_pressure",
        "time_to_throw",
    }
)


@dataclass(frozen=True, slots=True)
class BlockSnap:
    """One charted pass-rush snap, before it is attributed to a line.

    `slots=True` because there are ~22,000 of these held at once. No player
    list: see the module docstring — pressure is attributed to the unit, so
    the eleven-id column is projected away rather than parsed and discarded.
    """

    was_pressure: bool
    time_to_throw: float | None


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read. Also the ETag cache key."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_block_snaps(
    season: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> dict[tuple[str, int], BlockSnap]:
    """`(game_id, play_id) -> BlockSnap` for every charted pass-rush snap.

    **Not narrowed to a team, a week or a roster here.** All three need feeds
    this adapter does not read, and taking them as arguments creates the
    ordering trap described in `pbp.py`: a feed folded against a dependency
    that answered `304` yields a silently empty result the dependency's later
    re-fetch does not repair. The join is `capture`'s.

    Rows with no pass rushers are dropped as they are parsed — that is every
    run, kick and punt, and about half the document.

    Raises on an upstream failure rather than returning a short map.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    snaps: dict[tuple[str, int], BlockSnap] = {}

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        etag_key=url,
        **kwargs,
    ):
        rushers_raw = row["number_of_pass_rushers"].strip()
        if not rushers_raw:
            continue
        try:
            rushers = int(float(rushers_raw))
        except ValueError:
            continue
        if rushers <= 0:
            continue

        play_raw = row["play_id"].strip()
        if not play_raw.replace(".", "", 1).isdigit():
            continue
        game_id = row["nflverse_game_id"].strip()
        if not game_id:
            continue

        release_raw = row["time_to_throw"].strip()
        release: float | None
        try:
            release = float(release_raw) if release_raw else None
        except ValueError:
            release = None

        snaps[(game_id, int(float(play_raw)))] = BlockSnap(
            # `.upper() == "TRUE"`, never truthiness: this column's other
            # value is the STRING "FALSE", which is truthy.
            was_pressure=row["was_pressure"].strip().upper() == "TRUE",
            time_to_throw=release,
        )

    return snaps

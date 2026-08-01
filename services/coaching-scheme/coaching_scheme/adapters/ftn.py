"""FTN charting — pre-snap motion and play action, per (team, week).

Two fields, and they exist nowhere else free: `is_motion` and
`is_play_action`. Play-by-play carries `no_huddle` itself, so this feed's
`is_no_huddle` is deliberately **not** read — one authority per field, and the
one that is already being downloaded wins.

**7.75 MiB on the wire, and nflverse publishes no `.csv.gz` for it.** The
parquet variant is 0.53 MiB; see the README for why that saving did not buy
`pyarrow`. Measured through the shipped path: 0.5s for 47,316 rows.

**This feed does not say which team ran the play.** It carries
`nflverse_game_id` and `nflverse_play_id` and nothing about possession, so
every row is attributed through the offensive-play index `pbp.py` already
built. That is also a filter: a charted row with no entry in the index is a
special-teams or aborted play, and it is skipped rather than attributed to
whichever team the game id happens to name first.

Consequence worth stating: **this feed is useless without play-by-play.**
`capture` never reads it on the degraded branch, because the index it needs
is exactly what a missing play-by-play denies it.
"""

import os
from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

UPSTREAM_ADAPTER = "nflverse-ftn-charting"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "FTN_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/ftn_charting/"
    "ftn_charting_{season}.csv",
)

REQUIRED_COLUMNS = frozenset(
    {
        "nflverse_game_id",
        "nflverse_play_id",
        "is_motion",
        "is_play_action",
    }
)

TRUE = "TRUE"


@dataclass
class ChartingBucket:
    """One team-week's charted counters. Counts, never rates — see `pbp.py`."""

    charted_plays: int = 0
    motion_plays: int = 0
    play_action_plays: int = 0


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_charting_buckets(
    season: int,
    *,
    client: httpx.AsyncClient,
    play_index: dict[tuple[str, int], tuple[str, int]],
    etag_store=None,
) -> dict[tuple[str, int], ChartingBucket]:
    """`(team, week) -> ChartingBucket`, attributed through `play_index`.

    Raises on an upstream failure. `capture` treats that as degraded rather
    than fatal: `play_action_rate` and `pre_snap_motion_rate` become null with
    a reason and every other rate publishes, because those two are the only
    fields this feed owns.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    buckets: dict[tuple[str, int], ChartingBucket] = {}

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        etag_key=url,
        **kwargs,
    ):
        play_id_raw = row["nflverse_play_id"].strip()
        if not play_id_raw.replace(".", "").isdigit():
            continue
        located = play_index.get(
            (row["nflverse_game_id"].strip(), int(float(play_id_raw)))
        )
        if located is None:
            continue
        bucket = buckets.get(located)
        if bucket is None:
            bucket = buckets[located] = ChartingBucket()
        bucket.charted_plays += 1
        if row["is_motion"].strip().upper() == TRUE:
            bucket.motion_plays += 1
        if row["is_play_action"].strip().upper() == TRUE:
            bucket.play_action_plays += 1

    return buckets

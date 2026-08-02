"""Participation charting: pressure, rushers, release timing, front snaps.

The only module that knows this feed's wire format. It carries seven of this
collector's sixteen sourceable fields and the guard's whole independent
variable, so its loss ends the pass rather than nulling a column — see
`capture`.

--------------------------------------------------------------------------
Format, and why this is the one feed that is not gzipped
--------------------------------------------------------------------------

Re-checked live against the releases API for this build:

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
semicolon-joined 22-player lists per row and this adapter reads exactly one of
them; the other seven are most of the document's bulk.

--------------------------------------------------------------------------
Populated rates, measured live on the real 2025 season
--------------------------------------------------------------------------

Of 22,002 charted pass-rush snaps:

| column | populated | shape |
|---|---|---|
| `was_pressure` | 100% | `TRUE`/`FALSE` |
| `number_of_pass_rushers` | 100% | 0 on runs, 4 on a base rush, 5-6 on a blitz |
| `defense_players` | 100% | semicolon-joined GSIS ids |
| `time_to_throw` | 42.8% | dropbacks that were actually thrown |

`time_to_throw` being under half is correct rather than a gap: a sack, a
scramble and a throwaway have no release. It is charted on enough snaps that
`mean_time_to_throw_faced` lands in a `2.586-2.846`s band across the 32
defences, which is what the timing guard regresses against.

--------------------------------------------------------------------------
`was_pressure` is independent of the outcome, and stays that way
--------------------------------------------------------------------------

The spec requires pressure to be attributed to the rushing unit rather than
the play outcome, "so that hurries and knockdowns count even when the ball is
out". This feed charts `was_pressure` at the snap, entirely separately from
`sack`. Nothing here reads a sack, a completion or a yardage column, which is
the structural version of that requirement: there is no outcome in scope to
filter by.
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
        "defense_players",
    }
)


@dataclass(frozen=True, slots=True)
class RushSnap:
    """One charted pass-rush snap, before it is attributed to a defence.

    `slots=True` and interned `defenders` because there are ~22,000 of these
    held at once — this is the collector's largest resident structure and the
    reason it is a tuple of shared strings rather than a list of fresh ones.
    """

    rushers: int
    was_pressure: bool
    time_to_throw: float | None
    defenders: tuple[str, ...]


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Also the conditional-GET cache key, deliberately the same string.
    """
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_rush_snaps(
    season: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> dict[tuple[str, int], RushSnap]:
    """`(game_id, play_id) -> RushSnap` for every charted pass-rush snap.

    **Not narrowed to a defence, a week or a roster here.** All three need
    feeds this adapter does not read, and taking them as arguments is what
    creates the ordering trap described in `pbp.py`: a feed folded against a
    dependency that answered `304` yields a silently empty result that the
    dependency's later re-fetch does not repair. The join is `capture`'s, over
    a structure that is ~22,000 entries and a few megabytes.

    Rows with no pass rushers are dropped as they are parsed — that is every
    run, kick and punt, and about half the document.

    Raises on an upstream failure rather than returning a short map.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    snaps: dict[tuple[str, int], RushSnap] = {}
    # One shared string per player id across the whole document. Without it
    # ~22,000 rows x 11 defenders mint 240,000 separate 12-character strings.
    interned: dict[str, str] = {}

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

        defenders = tuple(
            interned.setdefault(player, player)
            for raw in row["defense_players"].split(";")
            if (player := raw.strip())
        )

        snaps[(game_id, int(float(play_raw)))] = RushSnap(
            rushers=rushers,
            # `.upper() == "TRUE"`, never truthiness: this column's other
            # value is the STRING "FALSE", which is truthy.
            was_pressure=row["was_pressure"].strip().upper() == "TRUE",
            time_to_throw=release,
            defenders=defenders,
        )

    return snaps

"""Participation charting — personnel groupings, per (team, week).

One field: `personnel_rates`. That is the whole return on **46.82 MiB**, the
largest upstream in this collector's set by a factor of two and a half, and
the reason its failure is a *degraded* pass rather than a fatal one — losing
one field of fourteen must not cost the other thirteen.

nflverse publishes no `.csv.gz` here either; the parquet is 4.52 MiB. See the
README for the decision. Measured through the shipped path, projected to 5 of
26 columns: 4.3s for 45,184 rows. The projection matters more here than
anywhere else in the fleet — the columns dropped include six semicolon-joined
22-player lists per row, which is most of the document's bulk.

`offense_personnel` reads like `"1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR"`.
Special-teams rows carry a defensive-looking grouping and no QB; they are
excluded by the same offensive-play index `ftn.py` uses rather than by
sniffing the string, so the two charted feeds agree on what a play is.
"""

import os
from dataclasses import dataclass, field

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
        "offense_personnel",
    }
)

# The four groupings the spec names, keyed `<running backs><tight ends>`, plus
# the catch-all. `heavy` is 4+ combined backs and tight ends — 22, 13 with a
# fullback, 23 — which is the usual definition and the one that keeps the five
# buckets from overlapping.
#
# The five do NOT sum to 1.0, deliberately: 10 personnel (1 RB, 0 TE, 4 WR) and
# 20 personnel are real and belong in neither. A residual is honest; forcing
# the five to close would push those snaps into whichever bucket was written
# last.
GROUPING_KEYS: tuple[str, ...] = ("p11", "p12", "p21", "p13", "heavy")
_EXPLICIT = {(1, 1): "p11", (1, 2): "p12", (2, 1): "p21", (1, 3): "p13"}
HEAVY_THRESHOLD = 4


@dataclass
class PersonnelBucket:
    """One team-week's personnel counters. Counts, never rates."""

    classified_plays: int = 0
    groupings: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(GROUPING_KEYS, 0)
    )


def classify(offense_personnel: str) -> str | None:
    """A personnel string to one of `GROUPING_KEYS`, or `None`.

    `None` means "counted as a classified play but in no named bucket" is
    *not* what happens — the caller skips it entirely, so an unrecognisable
    string cannot silently deflate every rate by inflating the denominator.
    """
    counts: dict[str, int] = {}
    for part in offense_personnel.split(","):
        part = part.strip()
        if not part:
            continue
        number, _, position = part.partition(" ")
        if not number.isdigit():
            return None
        counts[position.strip().upper()] = int(number)
    if not counts:
        return None
    # A genuine offensive grouping has a quarterback. Its absence is the
    # signature of the special-teams rows this feed mixes in, and of the
    # pre-snap rows with no personnel recorded at all.
    if counts.get("QB", 0) < 1:
        return None
    backs = counts.get("RB", 0) + counts.get("FB", 0)
    ends = counts.get("TE", 0)
    # **The named groupings win over `heavy`, and the order is load-bearing.**
    # 13 personnel is 1 back and 3 tight ends, which is 4 — so a `heavy` check
    # placed first swallows it and `p13` becomes unreachable. The spec names
    # p13 explicitly, so it is not a heavy package; `heavy` is the catch-all
    # for the unnamed 4+ groupings (22, 23) and nothing more.
    explicit = _EXPLICIT.get((backs, ends))
    if explicit is not None:
        return explicit
    if backs + ends >= HEAVY_THRESHOLD:
        return "heavy"
    return None


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_personnel_buckets(
    season: int,
    *,
    client: httpx.AsyncClient,
    play_index: dict[tuple[str, int], tuple[str, int]],
    etag_store=None,
) -> dict[tuple[str, int], PersonnelBucket]:
    """`(team, week) -> PersonnelBucket`, attributed through `play_index`.

    Raises on an upstream failure; `capture` degrades rather than failing.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    buckets: dict[tuple[str, int], PersonnelBucket] = {}

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        etag_key=url,
        **kwargs,
    ):
        play_id_raw = row["play_id"].strip()
        if not play_id_raw.replace(".", "").isdigit():
            continue
        located = play_index.get(
            (row["nflverse_game_id"].strip(), int(float(play_id_raw)))
        )
        if located is None:
            continue
        grouping = classify(row["offense_personnel"])
        if grouping is None:
            continue
        bucket = buckets.get(located)
        if bucket is None:
            bucket = buckets[located] = PersonnelBucket()
        bucket.classified_plays += 1
        bucket.groupings[grouping] += 1

    return buckets

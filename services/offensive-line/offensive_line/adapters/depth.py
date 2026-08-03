"""Depth charts: the LT/LG/C/RG/RT **label**, and nothing else.

This feed decides no membership. `snap_counts` decides who played; this one
says which of the five slots the label puts them in, which is what
`starter_position` publishes and what `lineup_hash` is ordered by. The spec's
warning — "or continuity becomes a description of the team's press releases" —
is about the first question, and this adapter is deliberately never asked it.

**One nuance, and it is the mechanism behind a real failure.** The chart is
not purely a label source: a man who took snaps but is absent from the chart
has no slot to occupy, so the chart is also a *candidacy filter*. That is
unavoidable — sidedness exists in no other free feed — but it means a
charting gap and a crosswalk gap both remove a genuine starter from the five,
which leaves the team's `lineup_hash` null and its change status unknowable.
`ratings.UNDETERMINABLE_CHANGE_REASON` is what the collector does about it.

--------------------------------------------------------------------------
Format, and the thing that nearly blocked the collector
--------------------------------------------------------------------------

| asset | updated | size |
|---|---|---|
| `depth_charts_2025.csv` | 2026-03-14T07:32:30Z | 50.47 MiB |
| `depth_charts_2025.csv.gz` | 2026-03-14T07:32:28Z | **10.15 MiB** |
| `depth_charts_2025.parquet` | 2026-03-14T07:32:28Z | 2.46 MiB |

Checked live for this build. The csv/csv.gz/parquet/rds group shares a
timestamp so the abandoned-artifact exception does not apply; only `.qs`, an R
serialisation this fleet never reads, lags. The fleet rule takes the `.csv.gz`
with `gzipped=True`, which also buys the truncation check.

**`pos_abb` really does carry sidedness.** Measured on the 2025 release: LT
21,068 rows, RT 19,097, RG 18,896, LG 18,815, C 17,974, with `pos_name`
spelling them out ("Left Tackle") and `gsis_id` populated on 548,638 of
554,215 rows. Without it the spec's five-valued `starter_position` enum would
have had to narrow to `T`/`G`/`C`, and `lineup_hash` would have had no
position order to be taken in.

**There is no `season` or `week` column.** `dt` is a daily snapshot instant —
219 distinct days across the 2025 release, from 2025-08-03 to 2026-03-14. So
"the depth chart as it stood in week 5" is a date lookup, and the date comes
from play-by-play's `game_date`. Reading the whole file's *latest* snapshot
instead would label a week-5 lineup with the March roster: mislabelled slots
would reorder the hash and report churn on lines that never changed.

554,215 rows x 12 columns, projected to 4 and filtered to the five line slots
as they are parsed — 106,829 rows kept on 2025, ~19% of the document.
"""

import bisect
import os
from dataclasses import dataclass, field

import httpx
from collector_core.streaming import stream_csv_dicts

from ..ratings import STARTER_POSITIONS

UPSTREAM_ADAPTER = "nflverse-depth-charts"

UPSTREAM_URL_TEMPLATE = os.getenv(
    "DEPTH_CHARTS_URL_TEMPLATE",
    "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/"
    "depth_charts_{season}.csv.gz",
)

REQUIRED_COLUMNS = frozenset({"dt", "team", "gsis_id", "pos_abb", "pos_rank"})

LINE_SLOTS = frozenset(STARTER_POSITIONS)


@dataclass
class DepthCharts:
    """Every line slot the feed publishes, bucketed by snapshot date.

    `snapshots` is `date -> (team, gsis_id) -> (pos_rank, position)`, and
    `dates` is its sorted keys so `labels_at` is a bisect rather than a scan
    of 219 days per lookup.

    **Every rank is kept, not just rank 1.** The rank says who the club
    *expects* to start; this feed is being asked only what slot a man plays,
    and a rank-3 tackle pressed into service by two injuries is still playing
    tackle. Keeping rank 1 alone would leave exactly the weeks a line was
    disrupted — the weeks this collector exists for — with fewer than five
    labelled slots and no lineup hash at all. The rank is retained only to
    break the tie when one player is listed at two slots in one snapshot, in
    which case the lower rank is his.
    """

    snapshots: dict[str, dict[tuple[str, str], tuple[int, str]]] = field(
        default_factory=dict
    )
    dates: list[str] = field(default_factory=list)

    def finalise(self) -> None:
        self.dates = sorted(self.snapshots)

    def labels_at(self, date: str | None) -> dict[tuple[str, str], str]:
        """`(team, gsis_id) -> LT|LG|C|RG|RT` as of the latest snapshot <= date.

        `date` of `None` — no calendar for that week — takes the **latest**
        snapshot rather than nothing: a label is better than no label, and the
        alternative is silently emitting no starter rows for the whole league
        because play-by-play happened not to carry a date.
        """
        if not self.dates:
            return {}
        if date is None:
            chosen = self.dates[-1]
        else:
            index = bisect.bisect_right(self.dates, date)
            # `index == 0` means every snapshot postdates the game. Use the
            # earliest, which is the closest thing to the chart as it stood.
            chosen = self.dates[0] if index == 0 else self.dates[index - 1]
        return {
            key: position for key, (_rank, position) in self.snapshots[chosen].items()
        }


def source_ref(season: int) -> str:
    """The exact upstream artifact this pass read. Also the ETag cache key."""
    return UPSTREAM_URL_TEMPLATE.format(season=season)


async def fetch_depth_charts(
    season: int,
    *,
    client: httpx.AsyncClient,
    etag_store=None,
) -> DepthCharts:
    """Every LT/LG/C/RG/RT listing in the season's depth-chart snapshots.

    Raises on an upstream failure. `capture` degrades the starter half of the
    collector rather than failing the pass: without labels there is no ordered
    hash, so the five starter rows and `lineup_hash` go to `coverage.missing`
    while the unit rows — which carry every rate and need nobody's name —
    still publish.
    """
    kwargs = {} if etag_store is None else {"etag_store": etag_store}
    url = source_ref(season)
    charts = DepthCharts()

    async for row in stream_csv_dicts(
        client,
        url,
        required_columns=REQUIRED_COLUMNS,
        columns=REQUIRED_COLUMNS,
        gzipped=True,
        etag_key=url,
        **kwargs,
    ):
        position = row["pos_abb"].strip().upper()
        if position not in LINE_SLOTS:
            continue
        gsis_id = row["gsis_id"].strip()
        team = row["team"].strip()
        if not gsis_id or not team:
            continue
        rank_raw = row["pos_rank"].strip()
        try:
            rank = int(float(rank_raw)) if rank_raw else 0
        except ValueError:
            continue
        if rank < 1:
            continue
        # `dt` is an ISO instant; the date part is the snapshot's identity and
        # is what a game date is compared against.
        date = row["dt"].strip()[:10]
        if len(date) != 10:
            continue

        snapshot = charts.snapshots.setdefault(date, {})
        key = (team, gsis_id)
        known = snapshot.get(key)
        # Lowest rank wins, so a man listed at two slots is assigned the one
        # the club ranks him higher in. Ties resolved by position order rather
        # than by row order, or the label would depend on how the CSV happened
        # to be written.
        if known is None or (rank, position) < known:
            snapshot[key] = (rank, position)

    charts.finalise()
    return charts

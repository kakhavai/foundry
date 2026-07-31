"""The upstream adapter — the only module that knows the wire format.

The feed is nflverse's published depth-chart asset, **one CSV per season**, and
it is not a snapshot: it is a *time series* of snapshots. The same player
appears once per `dt`, going back over the whole season. That shape is what
makes this collector's stability half computable at all — `weeks_at_rank` and
`rank_changes_4w` come out of the feed's own history rather than out of a lake
read, so one capture pass is self-contained.

**Memory is the hard constraint here, and it is not hypothetical.** The 2025
asset is **52.9 MB** (`Content-Length`, measured rather than assumed), and
`roster-scope` — the collector that reads the same feed — was `OOMKilled` at a
256Mi limit on its first deploy because a document of that size was held three
times over. Three rules follow, and all three are load-bearing:

1. **Stream.** `collector_core.streaming.stream_csv_dicts` yields one row at a
   time; peak memory is one chunk plus one row, independent of the file size.
2. **Filter as you parse.** Rows outside `universe.POSITIONS` are dropped at
   ingest. The feed carries the full two-deep for the offensive line, both
   defensive fronts and the rest of special teams; ~70% of it is dead weight
   here.
3. **Bound the retained history.** Even filtered, ~350 daily snapshots is far
   more than the four-week window the signals need. Only the newest `MAX_WEEKS`
   ISO-week buckets are kept per `(team, position)`, and within a bucket only
   the newest `dt`'s ordering. That caps retention at 160 groups x 6 weeks x a
   handful of players regardless of how far back the season file goes.

Rule 3 has no equivalent in `roster-scope`: it needs only the current chart, so
a last-write-wins dict bounds it for free. This collector needs history, so
"bounded by the config, not by the feed" has to be stated as a week count.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from collector_core.streaming import stream_csv_dicts

from ..universe import canonical_position, canonical_team, group_key

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "nflverse-depth-charts"

# A `{season}` template. The feed is published one asset per season, so a URL
# without the placeholder would silently pin every capture to whichever season
# was baked in — the same reasoning behind `player-projections`'
# `PROJECTIONS_SNAPSHOT_URL` carrying `{format}`.
UPSTREAM_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "depth_charts/depth_charts_{season}.csv"
)

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly with `reason=malformed`
# rather than map nulls into an append-only lake nobody rewrites. These are
# exactly the columns the mapping below reads; the feed carries several more
# (`espn_id`, `pos_grp`, `pos_id`, `pos_name`, `pos_slot`) this collector has no
# use for, and their absence is not an error.
#
# `stream_csv_dicts` asserts these against the header BEFORE yielding a single
# row, so a rename costs one HTTP round trip rather than half a million mapped
# nulls.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {"dt", "team", "player_name", "gsis_id", "pos_abb", "pos_rank"}
)

# How many ISO-week buckets of history to retain per `(team, position)`.
# `rank_changes_4w` needs four; six leaves margin for a bye week and for a feed
# that skips a publication week, without unbounding retention. Raising it raises
# peak memory linearly, so it is a declared constant rather than a tunable.
MAX_WEEKS = 6

# A ceiling on how many usable rows a single pass will map before giving up.
# Not a memory guard — streaming and `MAX_WEEKS` already bound that — but a
# guard against an upstream that starts serving something unbounded, so one
# capture cannot read forever inside its deadline. The 2025 asset is ~555,000
# rows in total, of which this collector keeps a small fraction.
MAX_ROWS = 5_000_000


class DepthChartTooLarge(ValueError):
    """The feed yielded more usable rows than `MAX_ROWS` before it finished."""


@dataclass(frozen=True)
class ChartEntry:
    """One charted player at one rank, exactly as the chart published them."""

    rank: int
    player_name: str
    gsis_id: str | None


@dataclass(frozen=True)
class WeekOrdering:
    """One `(team, position)` group's ordering as of one publication week.

    `captured_at` is the newest `dt` seen inside the bucket, which is what
    `chart_published_at` reports — *when the chart was published upstream*,
    never when this collector fetched it. The distinction is the whole point: a
    frozen chart re-served daily keeps its original `dt`, which is what makes
    the staleness check possible at all.
    """

    iso_year: int
    iso_week: int
    captured_at: datetime
    entries: tuple[ChartEntry, ...]


@dataclass(frozen=True)
class PositionHistory:
    """A `(team, position)` group's retained history, newest week first."""

    team: str
    position: str
    weeks: tuple[WeekOrdering, ...]

    @property
    def current(self) -> WeekOrdering:
        return self.weeks[0]


@dataclass(frozen=True)
class DepthChartFetch:
    """What one pass read, plus whether it had to stop early.

    `truncated` is carried rather than raised so a pass that ran out of budget
    still publishes what it resolved *and says so*. A truncated pass that
    reports itself truncated is useful; one that reports itself complete is the
    exact silent failure `coverage.expected` exists to catch.
    """

    histories: dict[str, PositionHistory]
    rows_kept: int
    truncated: bool = False


@dataclass
class _GroupAccumulator:
    """Folds one `(team, position)` group's rows into bounded week buckets.

    Retention is bounded by `MAX_WEEKS`, not by the feed. `_horizon` records the
    newest week bucket ever evicted so a later row belonging to it is ignored
    rather than resurrecting a partial ordering — the feed is published in `dt`
    order today, but relying on that would make retention silently wrong the
    first time it is not.
    """

    team: str
    position: str
    # (iso_year, iso_week) -> [newest dt in the bucket, {rank: entry}]
    buckets: dict[tuple[int, int], list] = field(default_factory=dict)
    horizon: tuple[int, int] | None = None

    def add(self, captured: datetime, rank: int, entry: ChartEntry) -> None:
        iso = captured.isocalendar()
        key = (iso.year, iso.week)

        if self.horizon is not None and key <= self.horizon:
            return

        if key not in self.buckets:
            if len(self.buckets) >= MAX_WEEKS:
                oldest = min(self.buckets)
                if key < oldest:
                    # Older than everything retained, and the buckets are full.
                    self.horizon = key if self.horizon is None else self.horizon
                    return
                del self.buckets[oldest]
                self.horizon = (
                    oldest if self.horizon is None else max(self.horizon, oldest)
                )
            self.buckets[key] = [captured, {}]

        bucket = self.buckets[key]
        if captured > bucket[0]:
            # A newer publication inside the same week supersedes the older one
            # wholesale. Merging them would splice two orderings together and
            # invent a chart nobody published.
            bucket[0] = captured
            bucket[1] = {}
        elif captured < bucket[0]:
            return
        bucket[1][rank] = entry

    def result(self) -> PositionHistory:
        """The retained history, newest week first.

        Non-optional, and that is an invariant rather than an oversight: an
        accumulator is only ever constructed immediately before an `add` that
        creates a bucket, and every early return in `add` requires a bucket to
        already exist. A `| None` return here would be a branch no test could
        reach, which is worse than no branch — it reads as a handled case while
        being dead code.
        """
        weeks = tuple(
            WeekOrdering(
                iso_year=key[0],
                iso_week=key[1],
                captured_at=value[0],
                entries=tuple(value[1][rank] for rank in sorted(value[1])),
            )
            for key, value in sorted(self.buckets.items(), reverse=True)
        )
        return PositionHistory(team=self.team, position=self.position, weeks=weeks)


def source_ref(season: int, week: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Not decorative: it is what makes a lake object reproducible, and what tells
    a reader which of two feeds a row came from a season later. `week` is unused
    — the feed is one asset per season and carries its own `dt` — and is kept in
    the signature so this matches the shape every collector's `source_ref` has.
    """
    return UPSTREAM_URL.format(season=season)


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def parse_dt(raw: str | None) -> datetime | None:
    """The feed's `dt` as an aware datetime, or None when it is unusable.

    A naive timestamp is read as UTC because that is what the feed publishes
    (`...Z`); an unparseable value returns None and the caller drops the row
    rather than substituting the fetch instant, which would make a frozen chart
    look freshly published — the precise failure this collector must detect.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def fetch_depth_charts(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    now: datetime,
    deadline: datetime | None = None,
) -> DepthChartFetch:
    """Stream the season's feed and fold it into bounded per-group histories.

    Raises on an upstream failure rather than returning an empty result: an
    empty result would be recorded as a successful capture of nothing, where an
    exception becomes a `present: 0` envelope with a populated `errors` array.
    """
    accumulators: dict[str, _GroupAccumulator] = {}
    rows_kept = 0
    truncated = False

    rows = stream_csv_dicts(
        client, source_ref(season, week), required_columns=REQUIRED_COLUMNS
    )
    async for row in rows:
        if rows_kept > MAX_ROWS:
            await rows.aclose()
            raise DepthChartTooLarge(
                f"depth chart feed yielded more than {MAX_ROWS} usable rows"
            )
        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Out of budget mid-stream. Stop reading, keep what folded, and say
            # so — `aclose()` releases the connection rather than leaving the
            # rest of a 53 MB body to drain in the background.
            truncated = True
            await rows.aclose()
            break

        team = canonical_team(row.get("team", ""))
        if team is None:
            continue
        position = canonical_position(row.get("pos_abb", ""))
        if position is None:
            continue
        rank = _int_or_none(row.get("pos_rank"))
        if rank is None:
            # An unordered row cannot occupy a ranked slot. Dropping it leaves a
            # hole that is visible; inventing an order would not be.
            continue
        name = (row.get("player_name") or "").strip()
        if not name:
            continue
        captured = parse_dt(row.get("dt"))
        if captured is None:
            continue

        key = group_key(team, position)
        accumulator = accumulators.get(key)
        if accumulator is None:
            accumulator = _GroupAccumulator(team=team, position=position)
            accumulators[key] = accumulator
        accumulator.add(
            captured,
            rank,
            ChartEntry(
                rank=rank,
                player_name=name,
                # A published crosswalk id, carried verbatim. It is the join key
                # a consumer uses against player-identity's `external_ids.gsis`
                # block — see `capture.build_chart_signal` for why `player_id`
                # itself is not minted here.
                gsis_id=(row.get("gsis_id") or "").strip() or None,
            ),
        )
        rows_kept += 1

    histories = {key: acc.result() for key, acc in accumulators.items()}
    return DepthChartFetch(
        histories=histories, rows_kept=rows_kept, truncated=truncated
    )

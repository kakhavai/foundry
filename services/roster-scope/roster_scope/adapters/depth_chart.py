"""Depth-chart adapter — ordered player references per team, with a capture
timestamp.

**The response is streamed and parsed line by line, never materialized.** The
candidate upstream's published file is ~37 MB. Reading it with `resp.text`
holds it three times over — httpx's buffered bytes, the decoded `str`, and the
`StringIO` copy handed to `csv` — which OOM-killed this collector's pod at a
256Mi limit on its first deploy (exit 137, CrashLoopBackOff, and probes
reporting `connection refused` because the process was simply gone).
Streaming keeps peak memory flat and independent of the file's size, so the
limit no longer has to be sized against an upstream nobody controls.

The feed is a **current snapshot**, not a per-week archive: it carries a `dt`
capture timestamp and no season or week column at all. So `season`/`week` are
not filters here — they scope the *envelope*, and the chart is simply "the
depth chart as of `dt`". `dt` becomes `depth_source_captured_at`, which is
what the 48-hour freshness invariant is checked against.

Worth stating plainly, because it is the collector's central caveat: depth
ordering is not a league-published fact. Teams publish charts for media
obligations, several sort alphabetically below the starter, and the ordering
that actually predicts snap share is closer to last week's usage than to any
published chart. `depth_rank` here means "where the chart put them", not
"how good they are".
"""

import csv
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from collector_core.conditional import ETAGS, UpstreamUnchanged, conditional_headers

from ..rules import ALL_RULES, MATCHUP_RULES, canonical_position, canonical_team

# The positions any rule actually demands -- the player scope's and the
# matchup scope's together -- derived from the rule sets rather than from
# "canonical_position recognises it". Those two questions were the same
# question only by accident, while the player scope was the only rule set
# `canonical_position` served. Widening `POSITION_ALIASES` for the matchup
# scope broke that coincidence, and the coincidence was load-bearing for
# memory: this adapter streams the ~37 MB feed described at the top of this
# module and must retain only what a rule demands, or the OOM this collector
# was killed by once already comes right back. Gating on rule demand instead
# of on recognition keeps this filter honest regardless of how wide
# `POSITION_ALIASES` grows.
#
# The union is safe, not just convenient: Task 6 builds the matchup list from
# this same feed, so the matchup positions (CB/S/LB/DL/OL) are genuinely
# needed downstream, not dead weight like the rest of the two-deep this
# filter still drops. Retention grows from ~416 slots' worth to ~1,024 --
# nowhere near the ~300k rows that caused the original OOM, so admitting them
# preserves the property that matters (retain only what a rule demands)
# rather than the narrower one (retain only what the player scope demands).
#
# The player scope and the matchup scope together already cover every
# canonical group `canonical_position` can produce, so today there is no
# *real* upstream label left that is recognised but unwanted -- "recognised"
# and "wanted" already compute the same function again, which has already
# erased the test coverage that once proved this gate (vs. `is None`)
# matters. `test_ingest_gates_on_configured_demand_not_on_recognition` in
# `tests/test_depth_chart_adapter.py` exists because of that, not as
# insurance against a hypothetical future rule set: it manufactures the gap
# synthetically since the config can no longer supply one.
#
# `DST` (from `TEAM_DEFENSE_RULE`) is inert in this set: `canonical_position`
# never returns it -- team defenses have no position label to canonicalize --
# so its presence here changes nothing. Harmless, just not load-bearing.
WANTED_POSITIONS: frozenset[str] = frozenset(
    rule.position for rule in (*ALL_RULES, *MATCHUP_RULES)
)

# A `{season}` template, mirroring `player-projections`' PROJECTIONS_SNAPSHOT_URL:
# the feed is published one asset per season, so a URL without the placeholder
# would silently pin every capture to whichever season was baked in.
DEPTH_CHART_URL = os.getenv(
    "DEPTH_CHART_URL",
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "depth_charts/depth_charts_{season}.csv",
)

# Schema-drift detection, per the Phase 8 failure-handling section: an upstream
# that renames a field must fail the capture loudly rather than map nulls into
# an append-only lake. These are the columns the mapping below actually reads;
# the feed carries several more (espn_id, pos_grp, pos_name, ...) that this
# collector has no use for.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {"dt", "team", "player_name", "pos_abb", "pos_rank"}
)

# A ceiling on how much we will pull before giving up. Not a memory guard —
# streaming already bounds that — but a guard against an upstream that starts
# serving something unbounded, so a capture cannot download forever inside its
# deadline.
MAX_DEPTH_CHART_CHARS = 256 * 1024 * 1024


class DepthChartSchemaError(ValueError):
    """The feed did not carry the columns the mapping depends on."""


class DepthChartTooLarge(ValueError):
    """The feed exceeded `MAX_DEPTH_CHART_CHARS` before it finished."""


@dataclass(frozen=True)
class DepthChartRow:
    """One row exactly as the chart published it.

    `name_raw` may still be a co-listing (`A OR B`) and `position_raw` is still
    the feed's own abbreviation (`WR`, `PK`, `LDE`, ...). Both are resolved in
    `scope.py`.
    """

    team: str
    position_raw: str
    depth_order: int
    name_raw: str
    jersey_number: int | None


@dataclass(frozen=True)
class DepthChart:
    team: str
    # Freshness of *this team's* chart. Per-team rather than one number for
    # the fetch, because one team's frozen chart is invisible in an aggregate
    # freshness average — which is the failure mode the spec names.
    captured_at: datetime
    rows: tuple[DepthChartRow, ...]


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_dt(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class _ChartAccumulator:
    """Folds rows into per-team charts as they stream past.

    **Bounded by the config, not by the feed.** Two filters do that, and both
    are load-bearing rather than tidiness:

    1. Rows whose position no rule demands (`WANTED_POSITIONS`) are dropped at
       ingest. The feed carries the full two-deep for every unit — offensive
       line, defense, special teams — and only some of that is ever consulted
       by a rule, so the rest is dead weight. Carrying it to preserve "the
       adapter does not decide what matters" cost more than the purity was
       worth. Gated on rule demand rather than on whether `canonical_position`
       merely recognises the label — recognising a label and wanting its rows
       are different questions once more than one rule set shares
       `canonical_position`.

    2. The feed is a **time series of snapshots**, not one snapshot: the same
       player appears once per `dt`, going back over the season. Only the
       newest row for each `(team, RAW position label, rank)` is kept, so
       what comes out is the *current* chart rather than its whole history.
       Raw label, not `canonical_position` -- see the `__init__` comment on
       `_newest` for why keying on the canonical form silently collapses
       distinct alias groups (`LT`/`LG`/`C`/`RG`/`RT` all -> `OL`) into one
       slot.

    Without both, streaming alone is not enough: the retained rows OOM-killed
    a 256Mi pod even after the body stopped being buffered. With them, the
    accumulator holds ~1,000 rows regardless of how large the feed grows.
    """

    def __init__(self, fetched_at: datetime) -> None:
        self._fetched_at = fetched_at
        # (team, RAW position label, rank) -> (row, dt). One entry per slot,
        # holding whichever snapshot is newest.
        #
        # Keyed on the raw label, not `canonical_position`. Task 4 widened
        # `POSITION_ALIASES` so several raw labels collapse into one
        # canonical group (`LT/LG/C/RG/RT` -> `OL`, `FS/SS` -> `S`,
        # `WLB/MLB/SLB` -> `LB`). A feed that labels each lineman
        # positionally -- `pos_rank` counted within its own label, so every
        # one of the five is rank 1 -- would hash all five to the same
        # `(team, "OL", 1)` key under the canonical form, and four of them
        # would silently lose to "newest wins" even though they are five
        # distinct, simultaneously-current players, not five snapshots of
        # one slot. Keying on the label the feed actually printed keeps
        # every raw label its own slot, so aliasing two labels into one
        # canonical group (a modelling decision made downstream, in
        # `ordered_candidates`/`resolve_matchup_slots`) can never again
        # collide two real rows at ingest. `result()` still reports
        # `position_raw` on every row, so nothing downstream needs to know
        # the key changed.
        self._newest: dict[tuple[str, str, int], tuple[DepthChartRow, datetime]] = {}
        self._freshest: dict[str, datetime] = {}
        self._index: dict[str, int] | None = None

    def set_header(self, fields: list[str]) -> None:
        missing = REQUIRED_COLUMNS - set(fields)
        if missing:
            raise DepthChartSchemaError(
                f"depth chart feed is missing column(s): {', '.join(sorted(missing))}"
            )
        self._index = {name: position for position, name in enumerate(fields)}

    def add(self, fields: list[str]) -> None:
        index = self._index
        if index is None:
            raise DepthChartSchemaError("depth chart row seen before its header")

        def get(column: str) -> str:
            position = index[column]
            return fields[position] if position < len(fields) else ""

        team = canonical_team(get("team"))
        if team is None:
            # A team the config does not know is dropped rather than passed
            # through: its slots then read as missing, which is the correct
            # accounting for "we have no chart for this team".
            return

        position_raw = get("pos_abb").strip()
        position = canonical_position(position_raw)
        if position not in WANTED_POSITIONS:
            # Dropped whether the label was unrecognised (`position is None`)
            # or recognised but not wanted by any rule here. Filter 1 in the
            # class docstring.
            return

        depth_order = _int_or_none(get("pos_rank"))
        if depth_order is None:
            # An unordered row cannot occupy a ranked slot. Dropping it leaves
            # the slot missing, which is visible; guessing an order would not be.
            return

        name = get("player_name").strip()
        if not name:
            return

        captured = _parse_dt(get("dt")) or self._fetched_at

        # Filter 2: newest snapshot wins for this slot. `>=` rather than `>`
        # so that when a feed repeats a `dt` the later line is authoritative,
        # matching how a reader scanning top-to-bottom would resolve it.
        # `position_raw`, not `position` (the canonicalized form) -- see the
        # `_newest` field comment in `__init__` for why keying on the
        # canonical group silently collapses distinct alias groups.
        key = (team, position_raw, depth_order)
        existing = self._newest.get(key)
        if existing is not None and captured < existing[1]:
            return

        self._newest[key] = (
            DepthChartRow(
                team=team,
                position_raw=position_raw,
                depth_order=depth_order,
                name_raw=name,
                # The feed carries no jersey number. Absent, not guessed —
                # `PlayerRef.jersey_number` stays None and the resolver simply
                # has one fewer attribute to match on.
                jersey_number=None,
            ),
            captured,
        )

    def result(self) -> dict[str, DepthChart]:
        rows_by_team: dict[str, list[DepthChartRow]] = {}
        for (team, _position_raw, _rank), (row, captured) in self._newest.items():
            rows_by_team.setdefault(team, []).append(row)
            # Freshness is taken from the rows actually kept, so it describes
            # the chart being returned rather than the oldest snapshot the
            # feed happened to contain.
            current = self._freshest.get(team)
            if current is None or captured > current:
                self._freshest[team] = captured

        return {
            team: DepthChart(
                team=team,
                # Falling back to the *fetch* instant rather than to a sentinel
                # is deliberate: a feed without the column is as fresh as the
                # fetch, and inventing an old timestamp would make every team
                # permanently stale.
                captured_at=self._freshest.get(team, self._fetched_at),
                rows=tuple(sorted(rows, key=lambda r: (r.position_raw, r.depth_order))),
            )
            for team, rows in rows_by_team.items()
        }


def _feed_row(line: str) -> list[str] | None:
    """One CSV line to fields.

    Parsed a line at a time rather than by handing `csv` the whole document,
    which is what keeps memory flat. The trade-off is that a quoted field
    containing a literal newline would be split; the feed's columns are
    timestamps, team codes, ids, position codes and player names, none of
    which can contain one.
    """
    line = line.rstrip("\r")
    if not line:
        return None
    return next(csv.reader([line]))


def parse_depth_chart_csv(text: str, *, fetched_at: datetime) -> dict[str, DepthChart]:
    """Parse a whole document. Convenience for tests and small inputs — the
    production path streams instead (`fetch_depth_charts`)."""
    accumulator = _ChartAccumulator(fetched_at)
    header_seen = False
    for line in text.splitlines():
        fields = _feed_row(line)
        if fields is None:
            continue
        if not header_seen:
            accumulator.set_header(fields)
            header_seen = True
            continue
        accumulator.add(fields)
    if not header_seen:
        raise DepthChartSchemaError("depth chart feed was empty")
    return accumulator.result()


def depth_chart_url(season: int) -> str:
    return DEPTH_CHART_URL.format(season=season)


async def fetch_depth_charts(
    season: int, week: int, client: httpx.AsyncClient, *, now: datetime
) -> dict[str, DepthChart]:
    """Stream the feed and fold it into per-team charts.

    `season` selects the asset; `week` is unused — the feed is a current
    snapshot (see the module docstring) and is kept in the signature so this
    adapter matches the shape every collector's fetch has.
    """
    url = depth_chart_url(season)
    accumulator = _ChartAccumulator(now)
    header_seen = False
    consumed = 0
    remainder = ""

    # The URL is the cache key: one ETag per season's asset. `depth_chart_url`
    # already returns exactly the string `capture.py` records as this
    # envelope's `upstream.source_ref`, so the key and the provenance cannot
    # drift apart. This collector does not route through `stream_csv_dicts`
    # (it streams and folds per-team charts itself, see the module
    # docstring), so the same conditional-GET shape is applied here directly.
    headers = conditional_headers(url, ETAGS)

    async with client.stream(
        "GET", url, follow_redirects=True, headers=headers
    ) as response:
        # Checked before `raise_for_status()` deliberately, matching
        # `stream_csv_dicts`: httpx only treats 4xx/5xx as errors, so a 304
        # would fall through today, but relying on that would make this
        # correct by accident.
        if response.status_code == 304:
            raise UpstreamUnchanged(url, source_ref=ETAGS.get(url))
        response.raise_for_status()
        ETAGS.set(url, response.headers.get("etag"))
        async for chunk in response.aiter_text():
            consumed += len(chunk)
            if consumed > MAX_DEPTH_CHART_CHARS:
                raise DepthChartTooLarge(
                    f"depth chart feed exceeded {MAX_DEPTH_CHART_CHARS} characters"
                )
            remainder += chunk
            lines = remainder.split("\n")
            # The last element is a partial line unless the chunk happened to
            # end on a boundary; either way it belongs to the next chunk.
            remainder = lines.pop()
            for line in lines:
                fields = _feed_row(line)
                if fields is None:
                    continue
                if not header_seen:
                    accumulator.set_header(fields)
                    header_seen = True
                    continue
                accumulator.add(fields)

    trailing = _feed_row(remainder)
    if trailing is not None:
        if header_seen:
            accumulator.add(trailing)
        else:
            accumulator.set_header(trailing)
            header_seen = True

    if not header_seen:
        raise DepthChartSchemaError("depth chart feed was empty")
    return accumulator.result()

"""The upstream adapter — the only module that knows the wire format.

Kept separate from `capture.py` so the orchestration (coverage accounting,
envelopes, the lake) can be tested against a fake upstream without mocking HTTP,
and so swapping the upstream touches one file.

**The upstream is read in two parts, and that is the design, not an accident.**

1. A small JSON **manifest** declaring `covers_from` / `covers_through` — the
   window the feed *acknowledges* having polled — plus the feed's own URL.
2. The **feed** itself, a CSV of transaction rows, streamed.

The split exists because of the one thing this collector's coverage cannot
infer. Coverage here is over polling windows (see `windows.py`), and a quiet
Tuesday with zero transactions is *full* coverage. If the covered window were
derived from the rows returned — earliest to latest `announced_at` — then a
genuinely quiet week would produce an empty window and read as a total
coverage failure, and a busy week would read as healthy no matter how much of
it the feed had actually skipped. Both directions are wrong. The phase doc's
word is **acknowledged**, and an acknowledgement is something an upstream
states, not something a consumer reconstructs. The manifest is where it states
it.

**Two memory rules, both learned the hard way.** roster-scope's first deploy was
OOMKilled at a 256Mi limit because a ~37 MB upstream document was buffered three
times over:

1. **Never hold an upstream response more than once.** `response.text` after
   `response.content`, or `json.loads(response.text)`, is two copies.
2. **Filter as you parse.** A season-long transaction wire is exactly the shape
   that bites: most of it is outside any one scoped week. `stream_csv_dicts`
   hands rows out one at a time, and the window check below `continue`s on the
   ones this pass is not keeping, so nothing outside the week is ever retained.

Only the manifest is read whole, and it is a handful of fields by construction.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import httpx
from collector_core.streaming import UpstreamSchemaError, stream_csv_dicts

from ..transactions import parse_timestamp
from ..windows import week_window

__all__ = [
    "REQUIRED_COLUMNS",
    "UPSTREAM_ADAPTER",
    "UPSTREAM_URL",
    "CoverageWindow",
    "fetch_manifest",
    "source_ref",
    "stream_rows",
]

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "roster-transactions-upstream"

# TODO: the real upstream — the official league transaction wire, ESPN's
# transactions endpoint, Sleeper, or nflverse roster diffs (all non-normative
# candidates in the phase doc). While this is empty the placeholder branch
# below runs, so a freshly scaffolded collector deploys, captures and serves
# before its upstream exists — the same stub-mode reasoning player-projections
# uses for `PROJECTIONS_SNAPSHOT_URL`.
UPSTREAM_URL = ""

# Asserted against the CSV header before a single row is yielded, so a renamed
# column fails the pass with `reason=schema` rather than mapping nulls into an
# append-only lake. Only the load-bearing columns are listed: the optional ones
# (`return_window_*`, `elevation_count_season`, `eligible_from_week`,
# `position`, `supersedes`, `void_reason`) are genuinely absent for most rows,
# so requiring them would fail a healthy feed.
REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "transaction_type",
        "player_id",
        "from_team",
        "to_team",
        "announced_at",
        "effective_at",
        "confidence",
        "is_void",
        "source_ref",
    }
)


class CoverageWindow:
    """The window the upstream acknowledges having polled.

    A tiny value object rather than a bare tuple because `covers_through` is
    the number the entire coverage block turns on, and a positional pair is one
    accidental swap away from reporting a healthy feed as a total outage.
    """

    __slots__ = ("covers_from", "covers_through", "feed_url")

    def __init__(
        self, covers_from: datetime, covers_through: datetime, feed_url: str
    ) -> None:
        if covers_through < covers_from:
            raise UpstreamSchemaError(
                f"manifest covers_through {covers_through.isoformat()} precedes "
                f"covers_from {covers_from.isoformat()}"
            )
        self.covers_from = covers_from
        self.covers_through = covers_through
        self.feed_url = feed_url


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Not decorative: it is what makes a lake object reproducible, and what tells
    a reader which of two feeds a row came from a season later. The manifest is
    the artifact named, because it is the one that carries the acknowledgement
    the coverage block is computed from.
    """
    if not UPSTREAM_URL:
        return None
    return f"{UPSTREAM_URL.rstrip('/')}/{season}/manifest.json"


def _placeholder_manifest(season: int, week: int, now: datetime) -> CoverageWindow:
    """A manifest acknowledging the whole elapsed week.

    DELETE ME with the placeholder branch in `stream_rows`. It acknowledges the
    full elapsed window rather than a partial one on purpose: a scaffolded
    collector with no upstream should report *honest* coverage — it really did
    cover everything its (empty) feed claimed — rather than an alarm nobody can
    act on.

    `max` because a scoped week can legitimately be in the *future*: a cluster
    running in July with `CAPTURE_WEEK=1` is polling a window that has not
    opened. Nothing has elapsed, so nothing can be acknowledged, and the
    watermark clamps to the window's own start rather than preceding it — which
    `CoverageWindow` refuses outright. The pass then reports coverage 0.0,
    which is the honest reading of "no interval of this week has been covered
    yet" and exactly what `windows.py` argues 0/0 must never be allowed to read
    as instead.
    """
    start, _ = week_window(season, week)
    return CoverageWindow(
        covers_from=start, covers_through=max(start, now), feed_url=""
    )


# DELETE ME along with the branch in `stream_rows`. Deterministic, offline, and
# shaped exactly like `normalize` expects, so the generated tests exercise the
# real orchestration rather than a mock. Timestamps are relative to the scoped
# week's own start so the rows always land inside whatever week is captured.
PLACEHOLDER_ROWS: tuple[dict[str, str], ...] = (
    {
        "transaction_type": "ps_elevation",
        "player_id": "fdy-placeholder-0001",
        "position": "WR",
        "from_team": "",
        "to_team": "KC",
        "offset_hours": "6",
        "effective_offset_hours": "30",
        "eligible_from_week": "",
        "elevation_count_season": "1",
        "confidence": "official",
        "is_void": "false",
        "void_reason": "",
        "supersedes": "",
        "source_ref": "placeholder/0001",
    },
    {
        "transaction_type": "ir_placement",
        "player_id": "fdy-placeholder-0002",
        "position": "RB",
        "from_team": "SF",
        "to_team": "",
        "offset_hours": "20",
        "effective_offset_hours": "20",
        "eligible_from_week": "",
        "elevation_count_season": "",
        "confidence": "reported",
        "is_void": "false",
        "void_reason": "",
        "supersedes": "",
        "source_ref": "placeholder/0002",
    },
    {
        "transaction_type": "signing",
        "player_id": "fdy-placeholder-0003",
        "position": "TE",
        "from_team": "",
        "to_team": "BUF",
        "offset_hours": "44",
        "effective_offset_hours": "68",
        "eligible_from_week": "2",
        "elevation_count_season": "",
        "confidence": "official",
        "is_void": "false",
        "void_reason": "",
        "supersedes": "",
        "source_ref": "placeholder/0003",
    },
)


async def fetch_manifest(
    season: int, week: int, *, client: httpx.AsyncClient, now: datetime
) -> CoverageWindow:
    """Read the feed manifest — the upstream's declared coverage window.

    Raises on failure rather than defaulting the window. A default would be the
    worst possible failure here: an unreachable manifest would silently claim
    the feed covered everything, and the coverage block that exists to catch
    exactly that would report 1.0.
    """
    if not UPSTREAM_URL:
        return _placeholder_manifest(season, week, now)

    response = await client.get(source_ref(season, week))
    response.raise_for_status()
    # Small by construction — three fields — so reading it whole is safe. The
    # streaming rules above are about the *feed*, which is the unbounded one.
    body = response.json()
    if not isinstance(body, dict):
        raise UpstreamSchemaError(
            f"manifest is a {type(body).__name__}, expected an object"
        )
    missing = {"covers_from", "covers_through", "feed_url"} - set(body)
    if missing:
        raise UpstreamSchemaError(
            f"manifest is missing field(s): {', '.join(sorted(missing))}"
        )
    return CoverageWindow(
        covers_from=parse_timestamp(body["covers_from"], "manifest.covers_from"),
        covers_through=parse_timestamp(
            body["covers_through"], "manifest.covers_through"
        ),
        feed_url=str(body["feed_url"]),
    )


async def stream_rows(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    window: CoverageWindow,
    now: datetime,
) -> AsyncIterator[dict[str, str]]:
    """Yield the feed's raw rows that fall inside the scoped week, one at a time.

    The week filter is applied **here, as the document is parsed**, not by the
    caller afterwards: a season-long wire is mostly rows this pass will not
    keep, and materialising them first is the roster-scope OOM verbatim. A row
    whose `announced_at` will not parse is yielded anyway rather than dropped
    silently — `capture.py` turns it into an explicit, counted failure, and a
    row quietly discarded by an adapter is indistinguishable from a row the
    upstream never sent.
    """
    if not UPSTREAM_URL:
        start, _ = week_window(season, week)
        for row in PLACEHOLDER_ROWS:
            yield _placeholder_row(row, start)
        return

    week_start, week_end = week_window(season, week)
    async for raw in stream_csv_dicts(
        client, window.feed_url, required_columns=REQUIRED_COLUMNS
    ):
        announced = (raw.get("announced_at") or "").strip()
        if announced:
            try:
                at = parse_timestamp(announced, "announced_at")
            except ValueError:
                yield raw
                continue
            if not (week_start <= at < week_end):
                continue
        yield raw


def _placeholder_row(row: dict[str, str], week_start: datetime) -> dict[str, str]:
    """Resolve a placeholder row's hour offsets into real timestamps."""
    resolved = {k: v for k, v in row.items() if not k.endswith("offset_hours")}
    resolved["announced_at"] = (
        week_start + timedelta(hours=int(row["offset_hours"]))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved["effective_at"] = (
        week_start + timedelta(hours=int(row["effective_offset_hours"]))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return resolved

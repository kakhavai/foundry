"""The capture pass: fetch -> history -> coverage -> envelope -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**.

Four things here are correctness rather than style.

--------------------------------------------------------------------------
1. `coverage.expected` never derives from what succeeded
--------------------------------------------------------------------------

`EXPECTED_FLOOR` encodes the size the universe is KNOWN to have — 272
regular-season games — independently of any fetch, and `CoverageAccumulator`
takes it as a floor that never lowers a genuine count. `acc.expect(game_id)` is
called because the feed LISTED the game; `acc.record` only after a window was
actually assigned to it.

The spec's own coverage sentence is the trap, and it is stated there rather
than hidden: *"one record per scheduled game in scope, with `window_id`
non-null; a game whose window is not yet assigned counts as missing rather
than defaulting to `sun_early`."* A game with no kickoff time still gets a
ROW — with `window_id: null` and a machine-readable reason — but it is a
coverage **miss**, never a plausible default.

--------------------------------------------------------------------------
2. The flex fields come from our own lake, and a history read that fails
   ends the pass
--------------------------------------------------------------------------

See `history.py`. The upstream publishes the current schedule and nothing
else, so `flex_status`, `previous_window_id` and `first_observed_at` are
derived by comparing this fetch against our own append-only snapshots. On a
first capture every game is `original`; fabricating a flex history from one
fetch is the failure.

A failed history read is routed into `fail_capture` rather than degraded to
"no history", because "no history" would publish a snapshot claiming
`original` for games previously recorded as flexed — into an append-only lake
where that claim becomes the evidence the next pass reads back.

--------------------------------------------------------------------------
3. `games_in_window` is computed only from a complete slate
--------------------------------------------------------------------------

The spec: *"a partial fetch produces a wrong value for every game in the
window, not just the missing one"*. `windows.build_slate` decides which weeks
can be shown to be complete, and for every game in a week that cannot,
`games_in_window`, `is_standalone` and `distribution` are **null with a
reason** rather than a plausible count. An undercount is the dangerous
direction: it manufactures standalone primetime games that do not exist.

--------------------------------------------------------------------------
4. A `weekly` cadence must not append an identical snapshot every day
--------------------------------------------------------------------------

The loop re-reads every 24 hours and the broadcast schedule changes a handful
of times a season, so publishing a byte-identical envelope on each pass would
fill an append-only lake with objects carrying no information. The pass
digests the rows it would publish and raises `UpstreamUnchanged` when they
match what this process last published AND SAW LAND.

That gate is also what keeps `history.read_history` affordable: the number of
snapshots in a season's partition is the number of times the broadcast
schedule actually moved, not the number of times we looked.

The digest is recorded only for a write `PublishResult.landed` confirms.
Recording one for content the lake never received would suppress the retry
until the upstream itself changed — `venue` reproduced exactly that, and it
takes two passes to see.
"""

import hashlib
import json
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.conditional import UpstreamUnchanged
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from .adapters.upstream import (
    UPSTREAM_ADAPTER,
    ScheduledGame,
    fetch_season_games,
    source_ref,
)
from .history import (
    FLEX_ORIGINAL,
    FlexVerdict,
    derive_flex,
    flex_is_evidenced,
    read_history,
)
from .metrics import metrics
from .windows import Slate, build_slate, classify_window, is_primetime

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL",
    "SIGNAL_TYPES",
    "build_signal",
    "capture_broadcast_context",
    "reset_published_digests",
]

COLLECTOR_NAME = "broadcast-context"
CADENCE_CLASS = CadenceClass.WEEKLY
SIGNAL = "game_broadcast_window"
SIGNAL_TYPES = (SIGNAL,)

# 272 regular-season games: 32 clubs x 17 games / 2. Declared here rather than
# counted from the fetch — that is the whole point. Postseason rows push the
# real count to 285 once they publish, and the floor never CAPS a genuine
# count, only raises a short one.
EXPECTED_FLOOR: dict[str, int] = {
    SIGNAL: 272,
}

# Coverage failure reasons. Named constants rather than literals: each implies
# a different operator action, and a test asserting on a typo'd string passes
# just as happily as one asserting on the real thing.
REASON_WINDOW_UNASSIGNED = "window_unassigned"
REASON_FLEX_HISTORY_UNEVIDENCED = "flex_history_unevidenced"
REASON_DEADLINE_EXCEEDED = "deadline_exceeded"

# Pass-level error reasons.
REASON_INCOMPLETE_SLATE = "incomplete_slate"
REASON_HISTORY_TRUNCATED = "history_truncated"

# Per-field null reasons. Emitted on the row in `null_field_reasons`, so a
# consumer can tell "null because there is nothing to say" from "null because
# nobody publishes it" without reading this file. The spec's own wording makes
# the distinction matter: it says `regional_coverage_pct` is "null for
# national" games, and here it is null for ALL of them — for two different
# reasons that must not collapse into one value.
NULL_NO_NETWORK_FEED = "no_free_feed_carries_network"
NULL_NO_REGIONAL_SOURCE = "no_free_regional_coverage_source"
NULL_NATIONAL_BROADCAST = "national_broadcast"
NULL_NO_VENUE_TIMEZONE = "venue_timezone_unavailable"
NULL_NO_PUBLICATION_INSTANT = "upstream_has_no_publication_instant"
NULL_NO_CHANGE_OBSERVED = "no_change_observed"
NULL_PREVIOUS_WINDOW_UNASSIGNED = "previous_window_unassigned"
NULL_INCOMPLETE_SLATE = REASON_INCOMPLETE_SLATE
NULL_KICKOFF_UNPUBLISHED = "kickoff_time_unpublished"
NULL_WINDOW_UNASSIGNED = REASON_WINDOW_UNASSIGNED

DISTRIBUTION_NATIONAL = "national"
DISTRIBUTION_REGIONAL = "regional"

BASIS_ANNOUNCED = "announced"
BASIS_FIRST_OBSERVED = "first_observed"

# `(season, week, signal_type) -> the digest THIS process last published AND
# saw land in the lake`. Per process, not read back from the lake: a pod
# restart finding a matching digest would raise `UpstreamUnchanged` against an
# EMPTY `CaptureState` and serve nothing from `/signals` until the schedule
# next moved. An in-memory digest costs exactly one redundant snapshot per
# restart, the same trade `ETagStore` makes for the same reason.
_PUBLISHED_DIGESTS: dict[tuple[int, int, str], str] = {}


def reset_published_digests() -> None:
    """Forget what this process has published. For tests only.

    A module-level dict outlives a test, so a second test asserting a real
    publish would otherwise get `UpstreamUnchanged` from the first one's
    leftovers and fail somewhere unrelated to what it was checking.
    """
    _PUBLISHED_DIGESTS.clear()


def _rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(payload: object) -> str:
    """A stable sha256 over anything JSON-serialisable.

    `sort_keys` so dict insertion order cannot change the digest, and
    `default=str` so a stray `datetime` renders rather than raising — a digest
    helper that throws would turn a cosmetic change into a capture failure.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _null_field_reasons(row: dict) -> dict[str, str]:
    """A reason for every field this row emits as null, and only those.

    Built by filtering a full table against the row rather than by appending
    to a dict as the row is assembled: the invariant a consumer relies on is
    "every null field is explained, and every explanation names a null field",
    and deriving both halves from the same table is what makes it hold by
    construction instead of by review.
    """
    table = {
        "window_id": NULL_WINDOW_UNASSIGNED,
        "network": NULL_NO_NETWORK_FEED,
        "distribution": NULL_INCOMPLETE_SLATE,
        "games_in_window": NULL_INCOMPLETE_SLATE,
        "is_standalone": NULL_INCOMPLETE_SLATE,
        "is_primetime": NULL_KICKOFF_UNPUBLISHED,
        "kickoff_at": NULL_KICKOFF_UNPUBLISHED,
        "kickoff_eastern_time": NULL_KICKOFF_UNPUBLISHED,
        "kickoff_local_time": NULL_NO_VENUE_TIMEZONE,
        "announced_at": NULL_NO_PUBLICATION_INSTANT,
        "regional_coverage_pct": (
            NULL_NATIONAL_BROADCAST
            if row["distribution"] == DISTRIBUTION_NATIONAL
            else NULL_NO_REGIONAL_SOURCE
        ),
        "previous_window_id": (
            NULL_NO_CHANGE_OBSERVED
            if row["flex_status"] == FLEX_ORIGINAL
            else NULL_PREVIOUS_WINDOW_UNASSIGNED
        ),
        "flex_decided_at": (
            NULL_NO_CHANGE_OBSERVED
            if row["flex_status"] == FLEX_ORIGINAL
            else NULL_NO_PUBLICATION_INSTANT
        ),
    }
    return {field: reason for field, reason in table.items() if row[field] is None}


def build_signal(
    game: ScheduledGame,
    *,
    window_id: str | None,
    slate: Slate,
    verdict: FlexVerdict,
) -> dict:
    """One scheduled game as a `game_broadcast_window` row.

    Mirrored by `contracts/signal-envelope/collectors/broadcast-context.json`,
    which `tests/test_capture_contract_conformance.py` validates the REAL
    output of this function against.

    Four of the spec's fields are null on every row this collector can build,
    and each carries its reason rather than a fabricated value:

    * `network` — the feed has no `network` and no `tv` column.
    * `kickoff_local_time` — the feed's clock is Eastern, and the venue-to-IANA
      table lives in a different service and a different Python package.
      `kickoff_eastern_time` is emitted instead, honestly named.
    * `announced_at` / `flex_decided_at` — no publication instant is
      published, and stamping the fetch time is what the spec forbids.
      `first_observed_at` carries the sound upper bound instead; see
      `history.py`.
    * `regional_coverage_pct` — the spec's own candidate source is a scraped
      image-based coverage map, not a feed.
    """
    kickoff_at = _rfc3339(game.kickoff_at)
    games_in_window = slate.games_in_window(game)
    is_standalone = None if games_in_window is None else games_in_window == 1
    distribution = (
        None
        if is_standalone is None
        else (DISTRIBUTION_NATIONAL if is_standalone else DISTRIBUTION_REGIONAL)
    )

    row = {
        "game_id": game.game_id,
        "season": game.season,
        "week": game.week,
        "game_type": game.game_type,
        "window_id": window_id,
        "network": None,
        "distribution": distribution,
        "games_in_window": games_in_window,
        "is_standalone": is_standalone,
        "is_primetime": is_primetime(game.kickoff_eastern),
        "kickoff_at": kickoff_at,
        "kickoff_eastern_time": (
            None
            if game.kickoff_eastern is None
            else game.kickoff_eastern.strftime("%H:%M")
        ),
        "kickoff_local_time": None,
        "regional_coverage_pct": None,
        "flex_status": verdict.flex_status,
        "previous_window_id": verdict.previous_window_id,
        "flex_decided_at": None,
        "announced_at": None,
        "first_observed_at": verdict.first_observed_at,
        "observed_window_count": verdict.observed_window_count,
        "point_in_time_basis": BASIS_FIRST_OBSERVED,
    }
    row["point_in_time_basis"] = (
        BASIS_ANNOUNCED if row["announced_at"] is not None else BASIS_FIRST_OBSERVED
    )
    row["null_field_reasons"] = _null_field_reasons(row)
    return row


def _build_envelope(
    games: list[ScheduledGame],
    history: dict,
    *,
    history_truncated: int,
    now: datetime,
    scope: dict,
    upstream: Upstream,
    deadline: datetime | None,
) -> tuple[Envelope, list[dict]]:
    """Every listed game as a row, with coverage accounted alongside."""
    slate = build_slate(games)
    acc = CoverageAccumulator(floor=EXPECTED_FLOOR[SIGNAL])
    now_iso = _rfc3339(now)

    rows: list[dict] = []
    unassigned = 0
    unevidenced = 0
    flexed = 0

    for game in games:
        # Expected because the feed LISTED the game, never because building a
        # row for it below happened to succeed.
        acc.expect(game.game_id)

        if deadline is not None and datetime.now(tz=UTC) >= deadline:
            # Over budget. Record the rest as missing rather than throwing away
            # what already resolved: a truncated pass that reports itself
            # truncated is useful; one that reports itself complete is not.
            acc.fail(game.game_id, REASON_DEADLINE_EXCEEDED)
            continue

        window_id = classify_window(
            kickoff_eastern=game.kickoff_eastern, game_type=game.game_type
        )
        verdict = derive_flex(
            history.get(game.game_id, ()),
            window_id=window_id,
            kickoff_at=_rfc3339(game.kickoff_at),
            now=now_iso,
        )

        if not flex_is_evidenced(verdict.flex_status, verdict.evidence):
            # The spec's consistency check, enforced at write time. A
            # non-`original` status with only one observed state is a record
            # claiming a history it cannot evidence, and it must not reach an
            # append-only lake where it would become that evidence. Derived
            # statuses cannot normally fail this, so reaching it means the
            # derivation is wrong — which is exactly when a plausible row is
            # most dangerous.
            unevidenced += 1
            acc.fail(game.game_id, REASON_FLEX_HISTORY_UNEVIDENCED)
            continue

        rows.append(
            build_signal(game, window_id=window_id, slate=slate, verdict=verdict)
        )
        if verdict.flex_status != FLEX_ORIGINAL:
            flexed += 1

        if window_id is None:
            # A row IS emitted — a consumer should be able to see the game
            # exists and that its window is not yet known — but it is a
            # coverage miss, because the spec counts an unassigned window as
            # missing rather than as a default.
            unassigned += 1
            acc.fail(game.game_id, REASON_WINDOW_UNASSIGNED)
            continue

        acc.record(game.game_id)

    if slate.incomplete_weeks:
        acc.add_error(
            REASON_INCOMPLETE_SLATE,
            "games_in_window/is_standalone/distribution withheld for week(s) "
            + ", ".join(str(week) for week in sorted(slate.incomplete_weeks)),
        )
    if history_truncated:
        acc.add_error(
            REASON_HISTORY_TRUNCATED,
            f"{history_truncated} snapshot(s) older than the newest "
            "MAX_HISTORY_SNAPSHOTS were not read",
        )

    metrics.rows_captured(len(rows))
    metrics.unassigned_windows(unassigned)
    metrics.incomplete_slate_weeks(len(slate.incomplete_weeks))
    metrics.unevidenced_flex_claims(unevidenced)
    metrics.flexed_games(flexed)

    envelope = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=COLLECTOR_NAME,
        signal_type=SIGNAL,
        captured_at=now,
        upstream=upstream,
        scope=scope,
        coverage=acc.result(),
        errors=acc.errors,
        signals=rows,
    )
    return envelope, rows


async def capture_broadcast_context(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one season's broadcast windows into one envelope.

    `week` is carried in the scope so the lake partitions the way every other
    collector's does, but the signal set is a whole season: `games_in_window`
    is a count over a slot's whole slate, which a week-scoped fetch cannot
    see. That is the shape `venue` and `schedule-context` use, for the same
    reason.
    """
    scope = {"season": season, "week": week}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )

    metrics.capture_attempt()
    try:
        # The upstream first, so the pod's readiness is never gated on
        # object-store latency, and so a `304` costs one round trip and no
        # lake reads at all.
        games = await fetch_season_games(season, client=client)
        history, history_truncated = await read_history(
            lake,
            collector=COLLECTOR_NAME,
            signal_type=SIGNAL,
            season=season,
            week=week,
        )
    except UpstreamUnchanged:
        # Not a failure: a 304 means the feed is byte-identical to the one this
        # process already read. Re-raised ABOVE the generic handler, or
        # `fail_capture` would write a `present: 0` envelope over a healthy
        # capture and count a failure that did not happen.
        raise
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Records `collector_capture_failures_total`, writes a `present: 0`
        # envelope, then re-raises. Never returns — do not add code after this
        # call, and do NOT call `metrics.capture_failure(exc)` first: the
        # library owns that counter for a failure that ends a pass.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=EXPECTED_FLOOR,
            source_ref=source_ref(season, week),
        )

    envelope, rows = _build_envelope(
        games,
        history,
        history_truncated=history_truncated,
        now=now,
        scope=scope,
        upstream=upstream,
        deadline=deadline,
    )

    digest = _digest(rows)
    if _PUBLISHED_DIGESTS.get((season, week, SIGNAL)) == digest:
        # Byte-identical to the last snapshot this process published and saw
        # land. `run_capture_loop` and `_run_capture` treat this as a
        # successful pass: `last_capture_at` advances and
        # `collector_upstream_unchanged_total` increments, while `/signals`
        # keeps serving the same rows.
        raise UpstreamUnchanged(
            source_ref(season, week) or COLLECTOR_NAME, source_ref=digest
        )

    # The shared tail: writes off the event loop, records the coverage gauge,
    # and records `collector_capture_failures_total` if the write fails — then
    # returns the envelope ANYWAY, because the capture succeeded and only its
    # archival copy did not.
    published = await publish_capture({SIGNAL: envelope}, lake=lake, metrics=metrics)

    # Gated on the write LANDING, and iterating `published` rather than a local
    # dict: `landed` raises for a signal type the call never published, which
    # is the caller having confused "unchanged" with "published and failed".
    for signal_type in published:
        if published.landed(signal_type):
            _PUBLISHED_DIGESTS[(season, week, signal_type)] = digest

    return published

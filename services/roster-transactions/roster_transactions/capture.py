"""roster-transactions's capture pass: manifest -> feed -> coverage -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**. A collector that reaches
its upstream inside a request handler has inverted that contract.

**Each pass re-reads the whole elapsed week, not a delta since the last poll.**
Two reasons, and both are structural. `CaptureState` is replaced wholesale by
each completed pass, so a delta-only capture would leave `/signals` showing
only the last fifteen minutes of a week. And transactions are *corrected* after
the fact — a `reported` row is superseded by an `official` one with a different
`effective_at`, and a rescinded move arrives as a follow-up `is_void` row — so
a collector that never re-reads an interval can never observe the correction to
it.

**`coverage.expected` counts polling windows, not transactions.** This is the
one place this collector differs from every other in the fleet, and the
reasoning is long enough to live in its own module: see `windows.py`. The short
version is that an event stream has no fixed cardinality — a quiet Tuesday
legitimately has zero rows — so `expected = len(rows)` would report a feed that
returned nothing as ratio 1.0. `expected` is therefore the set of 15-minute
intervals of the scoped week that have fully elapsed as of `now`, derived from
the calendar and the clock; `present` is the subset the upstream's manifest
acknowledged covering. Nothing the feed returns can shrink the expectation.

**A failed capture still writes an envelope.** `collector_core.failure.
fail_capture` writes one `present: 0` envelope per signal type with a populated
`errors` array, then re-raises. Both halves matter: the write makes a gap in the
append-only lake *explicit* rather than something a reader has to infer from
absence, and the re-raise stops `CaptureState` installing an empty capture over
the last good one. The floor handed to it is the elapsed-interval count, not the
static `EXPECTED_FLOOR` — a total outage during week 12 owes 672 intervals, and
flooring it to 1 would let the ratio read better than the truth.

Every lake call goes off the event loop via `awrite` — `LakeWriter` is
synchronous boto3, and the lake handed to this function raises if it is called
from the loop thread.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter, awrite

from .adapters.upstream import (
    UPSTREAM_ADAPTER,
    fetch_manifest,
    source_ref,
    stream_rows,
)
from .metrics import metrics
from .transactions import (
    TransactionSchemaError,
    UnknownTransactionType,
    duplicate_signing_count,
    normalize,
    parse_timestamp,
)
from .windows import (
    MIN_EXPECTED_INTERVALS,
    covered_interval_keys,
    elapsed_interval_keys,
    interval_key,
)

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "SIGNAL_TYPE",
    "capture_roster_transactions",
]

COLLECTOR_NAME = "roster-transactions"
CADENCE_CLASS = CadenceClass.VOLATILE
SIGNAL_TYPE = "roster_transaction"
SIGNAL_TYPES = (SIGNAL_TYPE,)

# The **static** minimum, and deliberately not the size of the universe — this
# collector's universe is a count of elapsed 15-minute intervals, which depends
# on `now` and so cannot be a constant. `expected_intervals()` below computes
# the real number per pass; this floor exists only to stop `expected == 0`
# (a week not yet started) reading as ratio 1.0. See `windows.py`.
EXPECTED_FLOOR: dict[str, int] = {
    SIGNAL_TYPE: MIN_EXPECTED_INTERVALS,
}

# Recorded in `coverage.missing` for an elapsed interval the upstream's manifest
# did not claim to have covered.
NOT_ACKNOWLEDGED = "window_not_acknowledged"


def _wall_clock() -> datetime:
    """Real elapsed time, for deadline enforcement only.

    Distinct from `capture_roster_transactions`'s `now`, which is the single
    instant the whole pass *describes* and is deliberately frozen for its
    duration. A deadline has to be checked against a clock that advances.
    """
    return datetime.now(tz=UTC)


def _row_interval_key(row: dict) -> str | None:
    """The interval a normalized row belongs to, or `None` if it cannot be
    placed.

    Placement is by `announced_at` — when the move entered the wire — rather
    than `effective_at`, because coverage is about whether the *poll* saw it.
    A move announced Monday and effective Thursday was available to a Monday
    poll, and filing it under Thursday would blame Monday's interval for a gap
    it did not have.
    """
    try:
        announced = parse_timestamp(row.get("announced_at"), "announced_at")
    except TransactionSchemaError:
        return None
    return interval_key(
        announced.replace(
            minute=(announced.minute // 15) * 15, second=0, microsecond=0
        )
    )


def _unplaceable_key(raw: dict, index: int) -> str:
    """A stable coverage key for a row that could not be mapped to an interval.

    Namespaced `row:` so a reader of `coverage.missing` can tell it from an
    `interval:` key. Prefers the upstream's own record pointer, because a
    positional index would change between passes and make the same broken row
    look newly missing every fifteen minutes.
    """
    pointer = (raw.get("source_ref") or "").strip()
    return f"row:{pointer or f'unidentified-{index}'}"


def expected_intervals(season: int, week: int, now: datetime) -> list[str]:
    """The elapsed intervals of the scoped week — this pass's owed universe.

    A named function rather than an inline call so the failure path and the
    success path provably use the same number: a failure envelope that floored
    to something smaller would report an outage as less bad than it was.
    """
    return elapsed_interval_keys(season, week, now)


async def capture_roster_transactions(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into a single `roster_transaction` envelope."""
    scope = {"season": season, "week": week}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )

    elapsed = expected_intervals(season, week, now)
    # Floored per pass at the elapsed count, so a failure reports the intervals
    # it genuinely owed rather than the static minimum.
    owed = {
        signal_type: max(len(elapsed), EXPECTED_FLOOR[signal_type])
        for signal_type in SIGNAL_TYPES
    }

    metrics.capture_attempt()
    try:
        window = await fetch_manifest(season, week, client=client, now=now)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        metrics.capture_failure(exc)
        # Writes a `present: 0` envelope per signal type, then re-raises `exc`.
        # Never returns — do not add code after this call.
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=owed,
            source_ref=source_ref(season, week),
        )

    # Declared up front from the clock — never from the document below. The
    # accumulator's own `floor` covers the not-yet-started-week case where
    # `elapsed` is empty; see `windows.py` for why 0/0 must not read as 1.0.
    acc = CoverageAccumulator(elapsed, floor=EXPECTED_FLOOR[SIGNAL_TYPE])
    acknowledged = set(covered_interval_keys(season, week, now, window.covers_through))
    for key in elapsed:
        if key not in acknowledged:
            acc.fail(key, NOT_ACKNOWLEDGED)

    signals: list[dict] = []
    poisoned: set[str] = set()
    unknown_types = 0
    truncated = False

    try:
        index = 0
        async for raw in stream_rows(
            season, week, client=client, window=window, now=now
        ):
            index += 1
            if deadline is not None and _wall_clock() >= deadline:
                # Over budget. Reported as truncated rather than thrown away:
                # a partial pass that says so is useful, one that reports
                # itself complete is not.
                truncated = True
                acc.add_error("deadline_exceeded", f"stopped after {index - 1} row(s)")
                break
            try:
                row = normalize(raw)
            except Exception as exc:  # noqa: BLE001 — one bad row is not a pass
                if isinstance(exc, UnknownTransactionType):
                    unknown_types += 1
                # Ambiguous rather than mapped: emitting a guess would put a
                # wrong move into an append-only lake. Counted in `missing`
                # with a reason instead, so the gap is explicit.
                acc.fail(_unplaceable_key(raw, index), metrics.reason_for(exc))
                placement = _row_interval_key(_safe_row(raw))
                if placement is not None:
                    poisoned.add(placement)
                continue
            signals.append(row)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        metrics.capture_failure(exc)
        await fail_capture(
            exc,
            collector=COLLECTOR_NAME,
            signal_types=SIGNAL_TYPES,
            adapter=UPSTREAM_ADAPTER,
            now=now,
            scope=scope,
            lake=lake,
            metrics=metrics,
            expected=owed,
            source_ref=source_ref(season, week),
        )

    # An interval whose rows would not map is not covered, whatever the
    # manifest claimed: the data for it is not trustworthy, and recording it
    # present would let a wholesale schema break read as full coverage with a
    # merely decorative `errors` array. A pass that ran out of budget claims
    # nothing at all — it stopped reading the feed partway, so it does not know
    # what the intervals it never reached contained.
    for key in sorted(acknowledged):
        if key in poisoned:
            acc.fail(key, "malformed")
        elif truncated:
            acc.fail(key, "deadline_exceeded")
        else:
            acc.record(key)

    metrics.rows_captured(len(signals))
    metrics.unknown_transaction_types(unknown_types)
    metrics.duplicate_signings(duplicate_signing_count(signals))

    envelope = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector=COLLECTOR_NAME,
        signal_type=SIGNAL_TYPE,
        captured_at=now,
        upstream=upstream,
        scope=scope,
        coverage=acc.result(),
        errors=acc.errors,
        signals=signals,
    )
    envelopes = {SIGNAL_TYPE: envelope}

    for signal_type, built in envelopes.items():
        await awrite(lake, built)
        metrics.coverage(signal_type, built.coverage.ratio)
    return envelopes


def _safe_row(raw: dict) -> dict:
    """The subset of a raw row `_row_interval_key` needs, without normalizing.

    A row that failed validation may still carry a parseable `announced_at`,
    and knowing which interval it poisoned is more useful than not knowing.
    """
    return {"announced_at": (raw.get("announced_at") or "").strip()}

"""The capture pass: fetch -> coverage -> envelopes -> lake.

`/signals` serves from the cache this fills, never from an upstream, so an
upstream outage costs **freshness, not availability**. A collector that reaches
its upstream inside a request handler has inverted that contract.

Two things here are correctness, not style, and both have a fleet-wide history:

**`coverage.expected` never derives from what succeeded.** A collector that
builds its expectation from the document it just fetched reports a truncated
upstream — 100 of 2,900 records — as `expected: 100, present: 100`, ratio 1.0.
Perfectly healthy, while 96% of the league silently vanished. `EXPECTED_FLOOR`
below encodes the size the universe is KNOWN to have, independently of the
fetch, and `CoverageAccumulator` takes it as a floor that never lowers a
genuine count. `acc.expect(key)` is called on the fact that made a key owed;
`acc.record(key)` only after it actually landed. Never the other way round.

**A failed capture still writes an envelope.** `collector_core.failure.
fail_capture` writes one `present: 0` envelope per signal type with a populated
`errors` array, then re-raises. Both halves matter: the write is what makes a
gap in the append-only lake *explicit* rather than something a reader has to
infer from absence, and the re-raise is what stops `CaptureState` installing an
empty capture over the last good one.

Every lake call goes off the event loop — `LakeWriter` is synchronous boto3,
and the lake handed to this function raises if it is called from the loop
thread. The success path reaches it through `publish_capture`, which also
decides what a failed write means: the capture worked and only its archival
copy did not, so the envelopes are returned and the failure is counted rather
than raised. Anywhere else, use `collector_core.lake`'s `awrite`/`aread`/
`alist_keys`, or `asyncio.to_thread` around a whole synchronous helper.
"""

from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.failure import fail_capture
from collector_core.lake import LakeWriter
from collector_core.publish import publish_capture

from .adapters.upstream import UPSTREAM_ADAPTER, fetch_rows, source_ref
from .metrics import metrics

__all__ = [
    "CADENCE_CLASS",
    "COLLECTOR_NAME",
    "EXPECTED_FLOOR",
    "SIGNAL_TYPES",
    "capture_defensive_front",
]

COLLECTOR_NAME = "defensive-front"
CADENCE_CLASS = CadenceClass.WEEKLY
SIGNAL_TYPES = ("defensive_front_strength",)

# TODO: the REAL size of this collector's universe, per signal type — 32
# teams, 272 games, 416 scope slots, ~2,900 rostered players, whatever it is
# here. Scaffolded to the placeholder adapter's row count so a fresh collector
# reports honest coverage on day one. Read the module docstring before you
# change how this number is produced: it must not come from the fetch.
EXPECTED_FLOOR: dict[str, int] = {
    "defensive_front_strength": 3,
}


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_key(row: dict) -> str:
    """The coverage key for one upstream row.

    It must be stable across passes and unique within one: it is what appears
    in `coverage.missing`, so a key that changes between passes makes every
    row look newly missing.
    """
    return str(row["key"])


def build_signal(signal_type: str, row: dict, *, now: datetime) -> dict:
    """One upstream row -> one signal row, for one signal type.

    TODO: this is the collector's actual product. Whatever shape you return,
    mirror it in this collector's schema under
    contracts/signal-envelope/collectors/.
    tests/test_capture_contract_conformance.py validates the REAL output of
    this function against that schema, so a renamed field fails there rather
    than in the generator six weeks later.

    The `key`/`observed_at`/`value` below and the schema that accepts them are
    placeholders that agree with each other by construction, which is why that
    conformance test currently proves only that two placeholders match. The
    schema carries a `$comment` marker so a collector reaching the repo with it
    still in place fails `tests/test_placeholder_schemas.py` — and rewriting
    the schema is what forces this function to follow.
    """
    return {
        "key": _row_key(row),
        "observed_at": _rfc3339(now),
        "value": row["value"],
    }


async def capture_defensive_front(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
    deadline: datetime | None = None,
) -> dict[str, Envelope]:
    """Capture one (season, week) into one envelope per signal type."""
    scope = {"season": season, "week": week}
    upstream = Upstream(
        adapter=UPSTREAM_ADAPTER,
        fetched_at=now,
        source_ref=source_ref(season, week),
    )

    metrics.capture_attempt()
    try:
        rows = await fetch_rows(season, week, client=client, now=now)
    except Exception as exc:  # noqa: BLE001 — classified, written, re-raised
        # Records `collector_capture_failures_total`, writes a `present: 0`
        # envelope per signal type, then re-raises `exc`. Never returns — do
        # not add code after this call, and do NOT call
        # `metrics.capture_failure(exc)` first: the library owns that counter
        # for a failure that ends a pass, and calling it here double-counts.
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

    envelopes: dict[str, Envelope] = {}
    for signal_type in SIGNAL_TYPES:
        acc = CoverageAccumulator(floor=EXPECTED_FLOOR[signal_type])
        signals: list[dict] = []
        for row in rows:
            key = _row_key(row)
            # Declared because the row EXISTS and is therefore owed — never
            # because building it below happened to succeed.
            acc.expect(key)
            if deadline is not None and datetime.now(tz=UTC) >= deadline:
                # Over budget. Record the rest as missing rather than throwing
                # away what already resolved: a truncated pass that reports
                # itself truncated is useful; one that reports itself complete
                # is not.
                acc.fail(key, "deadline_exceeded")
                continue
            try:
                signals.append(build_signal(signal_type, row, now=now))
            except Exception as exc:  # noqa: BLE001 — one bad row is not a pass
                acc.fail(key, metrics.reason_for(exc))
                continue
            acc.record(key)
        metrics.rows_captured(len(signals))
        envelopes[signal_type] = Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=signal_type,
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=acc.result(),
            errors=acc.errors,
            signals=signals,
        )

    # The shared tail: writes every envelope off the event loop, records each
    # coverage gauge, and records `collector_capture_failures_total` if a write
    # fails — then returns the envelopes ANYWAY. The capture succeeded; only
    # its archival copy did not, and an object-store outage must not cost
    # `/signals` a capture that is already built and correct. Do not replace
    # this with a hand-written `awrite` loop.
    return await publish_capture(envelopes, lake=lake, metrics=metrics)

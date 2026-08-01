"""The failed-capture path, once, for the whole fleet.

The Phase 8 contract is explicit: *"A poll that fails writes an envelope with
`coverage.present: 0` and a populated `errors` array. A gap in the lake is
therefore always explicit and never has to be inferred from absence — the
difference between 'we failed' and 'we never tried' is recorded rather than
reconstructed."*

`fail_capture` is both halves of that, and both are required.

**Write**, because an absent object and a failed capture are different facts
and must not look the same in an append-only lake nobody rewrites.

**Re-raise**, because `run_capture_loop` catches and leaves `CaptureState`
untouched. Returning the failure envelopes normally instead would hand them to
`CaptureState.apply_capture`, which would install them over the last good
capture — turning an upstream outage into a loss of *availability* on
`/signals`, when the whole point of capturing into a cache is that an outage
costs only freshness.

It also **records `collector_capture_failures_total` itself**, rather than
trusting each collector to remember it on every failure path. See
`publish.py`'s docstring for the other half of that decision, and for why the
*success* path answers the availability-vs-durability question the opposite
way: there the capture worked and only its archival copy did not.

Note what this deliberately does not do: it does not let the failure envelope's
coverage read as healthy. `Coverage.ratio` returns `1.0` when `expected` is 0 —
correct for a bye week that genuinely expects nothing, catastrophic for a
capture that failed before it could learn what to expect. Every signal type
therefore floors to at least `UNKNOWN_EXPECTED_FLOOR`, so a total outage
reports a ratio of 0.0 and `collector_coverage_ratio` alerts instead of
reading perfect.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import NoReturn

from .envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from .lake import LakeWriter, awrite
from .metrics import CollectorMetrics

logger = logging.getLogger(__name__)

# The floor applied to a failure envelope's `expected` when the caller has no
# better number — which is the normal case, because the failure is usually
# *why* the collector never learned what to expect. It must be at least 1:
# `expected: 0` makes `Coverage.ratio` read 1.0, and a total outage reporting
# perfect coverage is the precise failure the coverage block exists to catch.
UNKNOWN_EXPECTED_FLOOR = 1

# Upstream exception strings can carry a whole response body. Bounded so one
# failure cannot write a multi-megabyte lake object.
MAX_DETAIL_CHARS = 500


def failure_envelopes(
    exc: BaseException,
    *,
    collector: str,
    signal_types: Sequence[str],
    adapter: str,
    now: datetime,
    scope: Mapping,
    reason: str | None = None,
    expected: Mapping[str, int] | None = None,
    source_ref: str | None = None,
) -> dict[str, Envelope]:
    """Build one `present: 0` envelope per signal type. Writes nothing.

    Separated from `fail_capture` so a test can assert the shape without a
    lake, and so a collector with an unusual failure path can write them
    itself. `expected` names a per-signal-type floor for collectors that know
    one (`player-identity`'s ~2,900-record roster floor); anything unnamed —
    and anything named below `UNKNOWN_EXPECTED_FLOOR` — gets the floor.
    """
    detail = f"{type(exc).__name__}: {exc}"[:MAX_DETAIL_CHARS]
    classified = reason or CollectorMetrics.reason_for(exc)
    errors = [{"reason": classified, "detail": detail}]
    floors = dict(expected or {})

    return {
        signal_type: Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=collector,
            signal_type=signal_type,
            captured_at=now,
            upstream=Upstream(adapter=adapter, fetched_at=now, source_ref=source_ref),
            scope=dict(scope),
            coverage=Coverage(
                expected=max(
                    floors.get(signal_type, UNKNOWN_EXPECTED_FLOOR),
                    UNKNOWN_EXPECTED_FLOOR,
                ),
                present=0,
                missing=[],
            ),
            errors=list(errors),
            signals=[],
        )
        for signal_type in signal_types
    }


async def fail_capture(
    exc: BaseException,
    *,
    collector: str,
    signal_types: Sequence[str],
    adapter: str,
    now: datetime,
    scope: Mapping,
    lake: LakeWriter,
    metrics: CollectorMetrics,
    reason: str | None = None,
    expected: Mapping[str, int] | None = None,
    source_ref: str | None = None,
) -> NoReturn:
    """Write a failure envelope for every signal type, then re-raise `exc`.

    Never returns. The lake write goes through `awrite` (`asyncio.to_thread`),
    because this runs inside the capture coroutine and boto3 is synchronous.

    A lake write that itself fails is logged and swallowed: the original
    upstream failure is the one the caller must see, and shadowing it with a
    secondary S3 error would lose the fact that actually explains the pass.

    **This records `collector_capture_failures_total` itself.** Do not call
    `metrics.capture_failure(exc)` before calling this — that was the
    convention, every collector duplicated it, and a convention twenty-six
    authors must each remember is not a guarantee. A collector still records
    the counter for failures the library cannot see: a single bad row, one
    item's fetch inside a multi-call pass, a degraded path that builds its own
    envelopes rather than routing through here.

    **`reason` reaches Prometheus, not just the envelope.** It is handed to
    `metrics.capture_failure` as well as written into `errors`, so a fail-closed
    pass is labelled `scope_unavailable` rather than falling through the
    exception classifier to `unknown` — `ScopeUnavailable` is not an exception
    type `CollectorMetrics._reason` can recognise, and an alert that cannot tell
    "roster-scope published nothing" from an unclassified crash is not much of
    an alert. Passing it in both places is also what stops the two surfaces
    disagreeing about the same failure.
    """
    metrics.capture_failure(exc, reason=reason)

    envelopes = failure_envelopes(
        exc,
        collector=collector,
        signal_types=signal_types,
        adapter=adapter,
        now=now,
        scope=scope,
        reason=reason,
        expected=expected,
        source_ref=source_ref,
    )

    for signal_type, envelope in envelopes.items():
        try:
            await awrite(lake, envelope)
        except Exception:
            logger.exception(
                "failed to write the %s failure envelope for %s",
                signal_type,
                collector,
            )
        # Recorded even when the write failed: the gauge is what alerts, and
        # dropping it would make a lake outage look like a healthy collector.
        metrics.coverage(signal_type, envelope.coverage.ratio)

    raise exc

"""The successful-capture tail, once, for the whole fleet.

Every collector ended its `capture` with the same four lines: write each
envelope to the lake, record its coverage gauge, return the envelopes. Written
by hand nine times, and eight of those nine let a failed lake write escape:

    for signal_type, envelope in envelopes.items():
        await awrite(lake, envelope)                 # raises -> envelopes lost
        metrics.coverage(signal_type, envelope.coverage.ratio)
    return envelopes

That is a **contract inversion**. `CaptureState` exists so that an upstream
outage costs *freshness, not availability*; the capture above succeeded — the
envelopes are built, correct and in memory — and only the durable copy failed.
Letting that escape means `_run_capture`/`run_capture_loop` catch it,
`apply_capture` is never reached, and `/signals` serves the previous capture,
or nothing at all on a first run, while a perfectly good one sits in a local
variable. An **object-store** outage cost **availability**.

So the trade-off this module settles, deliberately:

**Availability wins over durability, and the durability failure is made
loud.** A lake write that fails is logged and recorded on
`collector_capture_failures_total`, and the envelopes are returned anyway. The
lake is append-only and resolved by recency, so the next successful pass writes
a superseding object; nothing about a missed write is unrecoverable. Refusing
to serve data the collector *has* is unrecoverable for every caller in the
meantime.

`failure.py` already made exactly this call for the failure path — a lake
outage there is logged and swallowed so it cannot shadow the upstream error
that actually explains the pass. This module makes the success path agree.

Note what this deliberately does **not** change. `fail_capture` still
re-raises, because there the *capture itself* failed: installing its
`present: 0` envelopes over the last good ones would destroy good data. "The
capture failed" and "the capture succeeded and only its archival copy failed"
are different facts and get different answers.

One consequence worth naming: a collector that derives state from its own last
lake object (`player-stats`'s revision counter) can now serve an in-memory
envelope whose revision the lake does not have. That was already true — the
write failed either way — and the alternative is serving nothing at all.
"""

import logging

from .envelope import Envelope
from .lake import LakeWriter, awrite
from .metrics import CollectorMetrics

logger = logging.getLogger(__name__)


async def publish_capture(
    envelopes: dict[str, Envelope],
    *,
    lake: LakeWriter,
    metrics: CollectorMetrics,
) -> dict[str, Envelope]:
    """Write a successful capture's envelopes, record coverage, return them.

    Never raises for a lake failure — see the module docstring. The write goes
    through `awrite` (`asyncio.to_thread`) because this runs inside the capture
    coroutine and boto3 is synchronous.

    `metrics.coverage` is recorded for every envelope whether or not its write
    landed: an absent Prometheus series and a healthy one are indistinguishable
    in PromQL, so a gauge that stops on a lake outage reads as a healthy
    collector.

    A failed write increments `collector_capture_failures_total` **here**,
    which is the point. `injury-report` watched a capture keep serving the last
    good data through an unresolvable MinIO endpoint while that counter stayed
    flat — an object-store outage was indistinguishable from a quiet cadence.
    Counting it in the library rather than asking twenty-six authors to
    remember is the same reasoning that made `EventLoopGuardedLake` raise
    rather than documenting a convention.
    """
    for signal_type, envelope in envelopes.items():
        try:
            await awrite(lake, envelope)
        except Exception as exc:  # noqa: BLE001 — durability, not availability
            logger.exception(
                "lake write failed for %s/%s; serving the capture from memory "
                "anyway and recording the failure",
                envelope.collector,
                signal_type,
            )
            metrics.capture_failure(exc)
        metrics.coverage(signal_type, envelope.coverage.ratio)
    return envelopes

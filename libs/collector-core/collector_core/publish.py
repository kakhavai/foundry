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

## Reporting the durability failure back: `PublishResult`

Swallowing the write failure is right for *availability*, and wrong for any
collector that keeps **state gated on the write having landed**. The case that
forced this is the digest gate three collectors run:

    if _PUBLISHED_DIGESTS.get(key) == digest:
        raise UpstreamUnchanged(...)      # suppress a byte-identical append
    ...
    _PUBLISHED_DIGESTS[key] = digest      # ← recorded even if the write failed

Record a digest for content the lake never received and the next pass digests
the same content, matches, raises `UpstreamUnchanged`, and **the object is
never written again until the upstream data itself changes** — which on a
`static reference` or `seasonal` cadence is months. One object-store blip costs
the season's object. Reproduced on `venue` before the fix: pass 1 with a
failing lake wrote nothing, pass 2 with a healthy lake raised
`UpstreamUnchanged` and still wrote nothing.

`venue`, `player-profile` and `durability-history` each worked around this with
an identical private `_WriteObserver` — a `LakeWriter` that delegates and
remembers which writes raised. The second copy's docstring named two as the
point to move it here; the third copy said so again. It lives here now.

`publish_capture` returns a `PublishResult`: a `dict[str, Envelope]` **subclass**,
so every caller that just returns it, compares it, or iterates it is unaffected
— which is why ten of the thirteen call sites needed no edit, including
`venue`'s own degraded tail. A caller that gates state on durability asks
`result.landed(signal_type)`.
"""

import logging

from .envelope import Envelope
from .lake import LakeWriter, awrite
from .metrics import CollectorMetrics

logger = logging.getLogger(__name__)


class PublishResult(dict[str, Envelope]):
    """The published envelopes, plus which of their lake writes did not land.

    A `dict[str, Envelope]` first and foremost: `publish_capture`'s contract is
    still "returns the envelopes", and a subclass keeps every existing caller
    — `return await publish_capture(...)`, `published == built`, `set(published)`
    — working untouched. The durability report is additive.

    `failed` names the signal types whose write raised. `landed` is its
    inverse, phrased the way a digest gate reads at the call site.
    """

    def __init__(
        self, envelopes: dict[str, Envelope], failed: frozenset[str] = frozenset()
    ) -> None:
        super().__init__(envelopes)
        self.failed = failed

    def landed(self, signal_type: str) -> bool:
        """Did this signal type's envelope reach the lake?

        Asking about a signal type this call never published raises, rather
        than answering. Both plausible defaults are wrong in a way that hides
        the bug: `True` records a digest for content that was never even
        offered to the lake — the exact permanent-suppression failure this
        class exists to prevent — and `False` silently reads as a durability
        outage that did not happen. The caller has confused "unchanged, so not
        published" with "published and failed", and those need different
        answers.

        **Iterate the result itself and the question cannot arise** — which is
        the point, because this raise is not free. It fires after every write
        has already happened, nothing in a collector catches it, and
        `_run_capture`/`run_capture_loop`'s blanket handler drops the pass: so
        `/signals` keeps serving the previous capture and `last_capture_at`
        stops advancing toward a staleness alert, **even though the lake write
        succeeded**. That is the same availability inversion this module exists
        to prevent, and the way to reintroduce it is to loop over your own
        `envelopes` dict — the natural slip, since `envelopes` and `digests`
        are both in scope at that point and `published` is the newcomer.
        """
        if signal_type not in self:
            raise KeyError(
                f"{signal_type!r} was not published by this call; "
                f"published: {sorted(self)}"
            )
        return signal_type not in self.failed


async def publish_capture(
    envelopes: dict[str, Envelope],
    *,
    lake: LakeWriter,
    metrics: CollectorMetrics,
) -> PublishResult:
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

    Returns a `PublishResult` — the envelopes, plus the signal types whose
    write did not land, for a caller that gates state on durability. See the
    module docstring.
    """
    failed: set[str] = set()
    for signal_type, envelope in envelopes.items():
        try:
            await awrite(lake, envelope)
        except Exception as exc:  # noqa: BLE001 — durability, not availability
            failed.add(signal_type)
            logger.exception(
                "lake write failed for %s/%s; serving the capture from memory "
                "anyway and recording the failure",
                envelope.collector,
                signal_type,
            )
            metrics.capture_failure(exc)
        metrics.coverage(signal_type, envelope.coverage.ratio)
    return PublishResult(envelopes, frozenset(failed))

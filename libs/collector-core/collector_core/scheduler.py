"""The capture loop: fleet machinery every collector shares.

A collector captures on a cadence and serves `/signals` from memory. Without
this loop the cache is only ever filled by `POST /refresh`, and `/signals`
serves an empty envelope indefinitely.

What each collector shapes for itself is *when its own event window is*: the
`next_event_at` callable a caller supplies digs the soonest still-relevant
event out of that collector's own signals (weather's kickoff time, betting's
game start, whatever a future collector's perishable moment is). Everything
else here — the loop, the escalation decision, staleness recording, surviving
a failed capture — is identical for every collector, so it lives here rather
than being reimplemented per service.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx

from .cadence import CadenceClass, next_interval
from .lake import LakeWriter
from .metrics import CollectorMetrics
from .routes import (
    DEFAULT_CAPTURE_DEADLINE,
    CaptureFn,
    CaptureState,
    _default_client_factory,
)

logger = logging.getLogger(__name__)

ESCALATE_WITHIN = timedelta(minutes=90)
ESCALATED_INTERVAL = timedelta(minutes=5)

# A collector's own lookup from its cached state to the soonest event still
# worth watching -- upcoming, or in progress. Returning a past timestamp is
# deliberate and expected: `next_interval` reads that as a negative delta and
# keeps the cadence escalated, because the window closes when the caller
# stops supplying an event (e.g. a game ending), not at the event's start.
NextEventAt = Callable[[CaptureState, datetime], datetime | None]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def interval_for_state(
    state: CaptureState,
    now: datetime,
    *,
    cadence_class: CadenceClass,
    next_event_at: NextEventAt,
    escalate_within: timedelta = ESCALATE_WITHIN,
    escalated_interval: timedelta = ESCALATED_INTERVAL,
) -> timedelta:
    """The interval until the next capture, honouring perishable escalation.

    The escalation decision itself -- including that a negative delta (an
    event already underway) stays escalated -- lives in `next_interval`. This
    function's only job is resolving the collector's own `next_event_at`
    against the current state before handing it over.
    """
    return next_interval(
        cadence_class,
        now=now,
        next_event_at=next_event_at(state, now),
        escalate_within=escalate_within,
        escalated_interval=escalated_interval,
    )


async def run_capture_loop(
    state: CaptureState,
    *,
    capture: CaptureFn,
    lake: LakeWriter,
    season: int,
    week: int,
    cadence_class: CadenceClass,
    next_event_at: NextEventAt,
    metrics: CollectorMetrics,
    escalate_within: timedelta = ESCALATE_WITHIN,
    escalated_interval: timedelta = ESCALATED_INTERVAL,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = _utc_now,
    capture_deadline: timedelta | None = DEFAULT_CAPTURE_DEADLINE,
    client_factory: Callable[[], httpx.AsyncClient] = _default_client_factory,
) -> None:
    """Capture forever, re-deriving the interval after each pass.

    A failed capture is logged and retried on the next tick rather than
    escaping the loop -- an upstream outage must degrade freshness, never
    take the service down. `capture` (e.g. weather's `capture_week`) already
    records the failure in its own envelope and metrics; this loop's only
    job on failure is to not die and to leave `state` exactly as it was
    before the attempt.

    `capture_deadline` bounds each pass the same way it bounds a dispatched
    `/refresh`: total upstream failure costs at most one deadline's worth of
    wall-clock time rather than `games x per-call timeout`, so an overrun
    pass cannot pile up behind the next tick indefinitely.

    `client_factory` matches `CollectorSpec.client_factory` -- previously this
    was hardcoded to `httpx.AsyncClient(timeout=10.0)` regardless of what a
    collector configured, so the knob a collector uses to own its transport
    silently did nothing on the path that performs virtually all captures.

    `state.lock` is held across the *whole* pass, not just the state
    assignment, so this loop and a dispatched `/refresh` can never call the
    upstream at the same time, and whichever finishes second cannot clobber
    a result that describes a later `now` (`CaptureState.apply_capture`).
    """
    process_started_at: datetime | None = None
    while True:
        now = clock()
        if process_started_at is None:
            process_started_at = now
        deadline = None if capture_deadline is None else now + capture_deadline
        try:
            async with state.lock:
                async with client_factory() as client:
                    envelopes = await capture(
                        season,
                        week,
                        client=client,
                        lake=lake,
                        now=now,
                        deadline=deadline,
                    )
                state.apply_capture(envelopes, now)
        except Exception:  # noqa: BLE001 -- the loop must survive anything
            logger.exception("capture failed; retrying on the next tick")

        if state.last_capture_at is not None:
            metrics.staleness((clock() - state.last_capture_at).total_seconds())
        else:
            # No capture has ever succeeded -- report elapsed-since-start
            # instead of skipping the metric outright, so
            # `collector_staleness_seconds` exists as a series from the first
            # tick. An absent series cannot page during exactly the total
            # outage this exists to catch.
            metrics.staleness((clock() - process_started_at).total_seconds())

        interval = interval_for_state(
            state,
            now,
            cadence_class=cadence_class,
            next_event_at=next_event_at,
            escalate_within=escalate_within,
            escalated_interval=escalated_interval,
        )
        await sleep(interval.total_seconds())

"""The five-route contract surface every collector serves identically.

`GET /health`, `GET /metrics`, `GET /catalog`, `GET /signals`, and
`POST /refresh` are fleet machinery: a generator that can consume one
collector can consume all of them, so this module is the whole
extensibility mechanism, not per-service boilerplate. A collector's own
extra routes (e.g. weather's `/signals/convergence`) are not part of this
contract and stay in the service.

`GET /signals` splits filtering in two. `season`, `week`, and `signal_type`
are universal — every envelope carries a `scope` and a `signal_type`, so the
router applies these itself. Everything else is collector-specific row
filtering, delegated to the spec's `signal_matches` predicate. A query
parameter that is neither universal nor declared in `supported_filters`
is rejected with 422 rather than silently ignored — the same reasoning
`player-projections` applies to `pos=FLEX`: a client bug should surface as
a loud error, not look like a quiet week.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .cadence import CadenceClass
from .envelope import ENVELOPE_VERSION, Envelope
from .lake import LakeWriter
from .metrics import CollectorMetrics
from .refresh import RefreshGate

logger = logging.getLogger(__name__)

# Applied by the router itself against every envelope, regardless of what a
# given collector declares in its own `supported_filters`.
UNIVERSAL_FILTERS: tuple[str, ...] = ("season", "week", "signal_type")

# A sane ceiling on how long a single capture pass may run before it must
# start truncating, applied both to the background loop and to a dispatched
# `/refresh`. Overridable per collector via `CollectorSpec.capture_deadline`.
DEFAULT_CAPTURE_DEADLINE = timedelta(seconds=300)


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0)


@dataclass
class CaptureState:
    """The in-memory cache `/signals` serves from and `/refresh` repopulates.

    A collector is never a synchronous pass-through to its upstream — an
    upstream outage degrades freshness, not availability — so this is the
    one place a running collector holds its latest captured envelopes.
    """

    envelopes: dict[str, Envelope] = field(default_factory=dict)
    last_capture_at: datetime | None = None
    # Strong references to dispatched-but-not-yet-finished `/refresh` captures.
    # `asyncio.create_task` does not itself keep a task alive -- with nothing
    # else referencing it, the task can be garbage-collected mid-flight. Kept
    # here so a pending capture survives until it finishes or is cancelled at
    # shutdown, and discarded via `add_done_callback` once it has.
    in_flight: set[asyncio.Task] = field(default_factory=set)
    # Serializes a background loop tick against a dispatched `/refresh` --
    # without it, both call `capture` concurrently (defeating the refresh
    # floor's purpose of not double-hitting the upstream) and whichever
    # finishes last wins the state assignment regardless of which of the two
    # actually captured later. Held across the *whole* capture, not just the
    # assignment.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def cancel_in_flight(self) -> None:
        """Cancel every dispatched capture still running and wait for it to
        unwind. Call from a collector's lifespan shutdown so a pending
        `/refresh` cannot outlive the app process."""
        tasks = list(self.in_flight)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def apply_capture(self, envelopes: dict[str, Envelope], now: datetime) -> None:
        """Install a completed capture's results.

        Belt-and-braces on top of `lock`: even serialized, a pass describing
        an older `now` must never overwrite one describing a newer `now` --
        e.g. a loop tick that started before a `/refresh` was dispatched but
        (were it not for `lock`) could otherwise finish after it. Call only
        while holding `lock`.
        """
        if self.last_capture_at is None or now > self.last_capture_at:
            self.envelopes = envelopes
            self.last_capture_at = now


# The capture entry point a collector supplies. Matches the shape of
# weather's `capture_week`: positional `season`, `week`, keyword-only
# `client`, `lake`, `now`, `deadline`, returning one Envelope per signal type.
CaptureFn = Callable[..., Awaitable[dict[str, Envelope]]]

# Collector-specific row filtering. The router hands each signal dict plus
# the query parameters it did not itself consume (the ones in
# `supported_filters` beyond `UNIVERSAL_FILTERS`) and lets the collector
# decide whether the row matches.
SignalMatcher = Callable[[dict, Mapping[str, str]], bool]


@dataclass
class CollectorSpec:
    """Everything the shared router needs to serve one collector's five
    standard routes. One instance per collector process."""

    name: str
    cadence_class: CadenceClass
    signal_types: tuple[str, ...]
    supported_filters: tuple[str, ...]
    capture: CaptureFn
    state: CaptureState
    lake: LakeWriter
    metrics: CollectorMetrics
    refresh_gate: RefreshGate
    signal_matches: SignalMatcher
    # The season/week a bare `POST /refresh` captures when the caller
    # supplies no scope. Owned per-collector rather than a shared library
    # literal: the loop's own scope comes from a collector's own env vars
    # (e.g. weather's CAPTURE_SEASON/CAPTURE_WEEK), and a hardcoded fleet-wide
    # default silently diverges from it the first time an operator advances
    # the week. The collector must populate this from the same source the
    # loop uses.
    default_scope: dict
    # The transport a dispatched `/refresh` capture opens its client with.
    # Defaulting to today's `httpx.AsyncClient(timeout=10.0)` means no
    # collector's behaviour changes; a collector whose upstream is slower or
    # rate-limited differently can override it without forking the router.
    client_factory: Callable[[], httpx.AsyncClient] = _default_client_factory
    # How long a single capture pass may run before `capture` must start
    # truncating it. `None` means uncapped -- today's behaviour.
    capture_deadline: timedelta | None = DEFAULT_CAPTURE_DEADLINE


def _rfc3339(value: datetime | None) -> str | None:
    return (
        None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


async def _run_capture(
    spec: CollectorSpec,
    season: int,
    week: int,
    clock: Callable[[], datetime] = _utc_now,
) -> None:
    """Run a dispatched capture. Never raises: an upstream failure must degrade
    freshness, not surface as an unhandled task exception. `spec.capture`
    already records the failure in its own envelope and metrics -- this is
    only a backstop for anything that escapes that, e.g. a bug in `capture`
    itself.

    The lock is taken *before* the clock is read: `now` (and the `deadline`
    derived from it) must describe when this capture actually started
    running, not when it was merely dispatched. Reading the clock first would
    let time spent waiting for the loop (or another `/refresh`) to release
    the lock silently eat into this capture's own deadline.
    """
    try:
        async with spec.state.lock:
            now = clock()
            deadline = (
                None if spec.capture_deadline is None else now + spec.capture_deadline
            )
            async with spec.client_factory() as client:
                envelopes = await spec.capture(
                    season,
                    week,
                    client=client,
                    lake=spec.lake,
                    now=now,
                    deadline=deadline,
                )
            spec.state.apply_capture(envelopes, now)
    except Exception:
        logger.exception("dispatched capture failed")


def build_collector_router(spec: CollectorSpec) -> APIRouter:
    """Build the mountable router for one collector's standard five routes."""
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    @router.get("/metrics")
    async def prometheus_metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @router.get("/catalog")
    async def catalog():
        """Self-description. The registry says a collector exists; this says
        what it currently offers."""
        return {
            "collector": spec.name,
            "envelope_version": ENVELOPE_VERSION,
            "cadence_class": str(spec.cadence_class),
            "signal_types": list(spec.signal_types),
            "filters": list(spec.supported_filters),
            "last_capture_at": _rfc3339(spec.state.last_capture_at),
            "coverage": {
                signal_type: envelope.coverage.to_dict()
                for signal_type, envelope in spec.state.envelopes.items()
            },
        }

    @router.get("/signals")
    async def signals(request: Request):
        query = dict(request.query_params)
        allowed = set(UNIVERSAL_FILTERS) | set(spec.supported_filters)
        unsupported = sorted(key for key in query if key not in allowed)
        if unsupported:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported filter(s): {', '.join(unsupported)}; "
                f"expected one of {', '.join(sorted(allowed))}",
            )

        signal_type = query.get("signal_type")
        if signal_type is not None and signal_type not in spec.signal_types:
            raise HTTPException(
                status_code=422,
                detail=f"unknown signal_type {signal_type!r}; "
                f"expected one of {', '.join(spec.signal_types)}",
            )

        try:
            season = int(query["season"]) if "season" in query else None
            week = int(query["week"]) if "week" in query else None
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"season/week must be integers: {exc}"
            ) from None

        collector_params = {
            key: value for key, value in query.items() if key not in UNIVERSAL_FILTERS
        }

        wanted = spec.signal_types if signal_type is None else (signal_type,)
        envelopes = []
        for name in wanted:
            envelope = spec.state.envelopes.get(name)
            if envelope is None:
                continue
            if season is not None and envelope.scope.get("season") != season:
                continue
            if week is not None and envelope.scope.get("week") != week:
                continue
            body = envelope.to_dict()
            body["signals"] = [
                row
                for row in body["signals"]
                if spec.signal_matches(row, collector_params)
            ]
            envelopes.append(body)
        return {"envelopes": envelopes, "count": len(envelopes)}

    @router.post("/refresh", status_code=202)
    async def refresh(body: dict | None = None):
        """Force a capture outside the cadence, subject to the interval floor.

        Returns 202 once the capture is dispatched, not once it finishes --
        the caller is told "accepted", not "done". The capture itself runs
        as a background task and lands in `spec.state` (and the lake, via
        `spec.capture`) whenever it completes.
        """
        now = datetime.now(tz=UTC)
        refresh_id = spec.refresh_gate.try_acquire(now)
        if refresh_id is None:
            return JSONResponse(
                {"detail": "refresh requested too soon"},
                status_code=429,
                headers={"Retry-After": str(spec.refresh_gate.retry_after(now))},
            )

        scope = body or {}
        season = int(scope.get("season", spec.default_scope["season"]))
        week = int(scope.get("week", spec.default_scope["week"]))

        # `_run_capture` reads its own clock after it acquires `spec.state.lock`
        # -- `now` here is deliberately *not* passed through to it. This `now`
        # is only for the refresh floor, the refresh id, and the response
        # body, all of which are properly measured from when the request
        # arrived, not from whenever the dispatched capture eventually runs.
        task = asyncio.create_task(_run_capture(spec, season, week))
        spec.state.in_flight.add(task)
        task.add_done_callback(spec.state.in_flight.discard)

        return {"refresh_id": refresh_id, "scope": {"season": season, "week": week}}

    return router

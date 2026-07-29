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

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .cadence import CadenceClass
from .envelope import ENVELOPE_VERSION, Envelope
from .lake import LakeWriter
from .metrics import CollectorMetrics
from .refresh import RefreshGate

# Applied by the router itself against every envelope, regardless of what a
# given collector declares in its own `supported_filters`.
UNIVERSAL_FILTERS: tuple[str, ...] = ("season", "week", "signal_type")

# The season/week a bare `POST /refresh` captures when the caller supplies no
# scope. Football-calendar concepts, not weather-specific — every collector
# in this fleet shares one season/week domain. Hardcoded pending a real
# "current scope" lookup; moving that literal is out of scope for this task.
_DEFAULT_SEASON = 2026
_DEFAULT_WEEK = 1


@dataclass
class CaptureState:
    """The in-memory cache `/signals` serves from and `/refresh` repopulates.

    A collector is never a synchronous pass-through to its upstream — an
    upstream outage degrades freshness, not availability — so this is the
    one place a running collector holds its latest captured envelopes.
    """

    envelopes: dict[str, Envelope] = field(default_factory=dict)
    last_capture_at: datetime | None = None


# The capture entry point a collector supplies. Matches the shape of
# weather's `capture_week`: positional `season`, `week`, keyword-only
# `client`, `lake`, `now`, returning one Envelope per signal type.
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


def _rfc3339(value: datetime | None) -> str | None:
    return (
        None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


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
        """Force a capture outside the cadence, subject to the interval floor."""
        now = datetime.now(tz=UTC)
        refresh_id = spec.refresh_gate.try_acquire(now)
        if refresh_id is None:
            return JSONResponse(
                {"detail": "refresh requested too soon"},
                status_code=429,
                headers={"Retry-After": str(spec.refresh_gate.retry_after(now))},
            )

        scope = body or {}
        season = int(scope.get("season", _DEFAULT_SEASON))
        week = int(scope.get("week", _DEFAULT_WEEK))
        async with httpx.AsyncClient(timeout=10.0) as client:
            envelopes = await spec.capture(
                season, week, client=client, lake=spec.lake, now=now
            )
        spec.state.envelopes = envelopes
        spec.state.last_capture_at = now
        return {"refresh_id": refresh_id, "scope": {"season": season, "week": week}}

    return router

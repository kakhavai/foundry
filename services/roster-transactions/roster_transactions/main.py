"""roster-transactions's process wiring: the descriptor, plus `GET /events`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException, Query

from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPE,
    SIGNAL_TYPES,
    capture_roster_transactions,
)
from .events import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, page
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_roster_transactions,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Never pass a callable here — an
        # already-bound function defeats the guard while every test stays green.
        #
        # No `next_event_at`: transactions have no perishable moment to
        # escalate toward. Every interval matters equally, which is exactly why
        # coverage counts intervals rather than events.
    )
)


@app.get("/events")
async def events(
    since: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    """Cursor-paged event stream — this collector's one route beyond the five.

    Served from the capture cache via `app.state.collector_spec`, never a
    module-level global: the routes must see the objects the last capture
    installed, not the ones that existed at import.
    """
    envelope = app.state.collector_spec.state.envelopes.get(SIGNAL_TYPE)
    rows = [] if envelope is None else envelope.signals
    try:
        return page(rows, since=since, limit=limit)
    except InvalidCursor as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

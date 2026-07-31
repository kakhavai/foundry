"""depth-chart's process wiring: the descriptor and `/signals/diff`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. The diff's own logic lives in `diff.py`, so the route body
here stays a parse and a delegate.
"""

import asyncio

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import HTTPException, Query

from .capture import (
    CADENCE_CLASS,
    CHART_SIGNAL,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    capture_depth_chart,
)
from .diff import SnapshotNotFound, build_diff
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_depth_chart,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`.
        # No `next_event_at`: the volatile cadence's base interval is the whole
        # schedule — a depth chart has no single perishable moment to escalate
        # toward the way a kickoff does.
    )
)


@app.get("/signals/diff")
async def signals_diff(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    season: int = 2026,
    week: int = 1,
):
    """Ordering changes between two captures, per the Phase 8 spec.

    Offloaded whole rather than awaiting each lake call: `build_diff` is
    synchronous and does one `list_keys` plus two `read`s, so running it on the
    event loop would stall every other request — including `/health` — for the
    duration of a prefix scan. The guarded lake makes forgetting an error rather
    than a stall.
    """
    spec = app.state.collector_spec
    try:
        return await asyncio.to_thread(
            build_diff,
            spec.lake,
            spec.name,
            CHART_SIGNAL,
            season,
            week,
            from_captured_at=from_,
            to_captured_at=to,
        )
    except SnapshotNotFound as exc:
        # 404, not an empty diff: "nothing changed" and "that capture never
        # happened" are different answers and must not look the same.
        raise HTTPException(status_code=404, detail=str(exc)) from None

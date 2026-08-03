"""Process wiring: the descriptor, and `/lineups`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

import asyncio

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import Query

from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    STRENGTH,
    capture_offensive_line,
)
from .lineups import build_lineup_view
from .metrics import metrics
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_offensive_line,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard. Do not write a telemetry.py, and
        # never pass a callable here — an already-bound function defeats the
        # guard while every test stays green.
        #
        # No `next_event_at`: the loop runs on its cadence class's base
        # interval and never escalates. The spec's "within 6h of each game's
        # final, plus a daily 09:00 UTC refresh" describes a cadence, not a
        # perishable instant; `weekly` already polls more often than that, and
        # an escalation hook needs a moment to escalate *toward*. A line
        # rating has none — weather's kickoff is still the fleet's only one.
    )
)


# Routes beyond the standard five go below this line. Published in this
# collector's Helm values under `gateway.publicPaths`, or it 404s at the
# gateway while working in-cluster.
@app.get("/lineups")
async def lineups(
    season: int = Query(...),
    week: int = Query(...),
    team: str | None = Query(None),
):
    """The projected starting five and its continuity for an upcoming week.

    Forward-looking, which is why the spec gives it a route rather than
    leaving it to `/signals`: the availabilities on these rows describe
    `week + 1`, and `unavailable_starters` names the men a generator should
    not project as playing. Neither is derivable from the realized history
    `/signals` serves without already knowing which of its fields look
    forward.

    Reaches the lake and the collector name through `app.state.collector_spec`,
    never a module-level global — the route must see the same objects a
    capture just replaced.

    Offloaded whole rather than awaiting each lake call individually:
    `build_lineup_view` does one `list_keys` plus one `read`, so running it on
    the event loop would stall every other request — including `/health` — for
    the duration of a prefix scan. One `to_thread` moves all of it off, and the
    guarded lake makes forgetting an error rather than a stall.
    """
    spec = app.state.collector_spec
    view = await asyncio.to_thread(
        build_lineup_view, spec.lake, spec.name, STRENGTH, season, week, team
    )
    return {"season": season, "week": week, "lineups": view, "count": len(view)}

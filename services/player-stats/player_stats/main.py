"""player-stats' process wiring: the descriptor, and `GET /revisions`.

Everything else — environment parsing, `CaptureState`, the capture loop, bearer
auth, the OTel guard and the standard five routes — lives in
`collector_core.app`. If you find yourself writing any of it here, it already
exists; see docs/collectors.md.
"""

from collector_core.app import CollectorDescriptor, build_collector_app

from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    capture_player_stats,
)
from .metrics import metrics
from .revisions import revisions_view
from .signals import SUPPORTED_FILTERS, signal_matches

app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_player_stats,
        signal_matches=signal_matches,
        metrics=metrics,
        # No `telemetry_module`: it defaults to `collector_core.telemetry`, the
        # fleet's shared wiring, resolved by importlib INSIDE the
        # OTEL_EXPORTER_OTLP_ENDPOINT guard.
        #
        # No `next_event_at`: a box score is not perishable. It is published
        # once a game ends and only ever restated afterwards, so the weekly
        # cadence's base interval is the whole schedule and there is nothing to
        # escalate toward.
    )
)


@app.get("/revisions")
async def revisions(
    since: str | None = None, season: int | None = None, week: int | None = None
):
    """Restated `(game_id, player_id, revision)` tuples, so the generator can
    invalidate cached features without re-reading the whole week.

    Validation, the lake scan and the payload shaping all live in
    `revisions.py`; this reaches the lake and the collector name through
    `app.state.collector_spec` rather than a module-level global, so the route
    sees the same objects a capture just replaced.
    """
    return await revisions_view(
        app.state.collector_spec, since=since, season=season, week=week
    )

"""weather's process wiring: the descriptor, `_signal_matches`, and
`/signals/convergence`. Everything else lives in `collector_core.app`.
"""

from collector_core.app import CollectorDescriptor, build_collector_app
from fastapi import Query

from .capture import CADENCE_CLASS, COLLECTOR_NAME, SIGNAL_TYPES, capture_week
from .convergence import build_convergence_series
from .metrics import metrics
from .scheduler import next_kickoff

# weather emits no player_id -- accepting it silently would return everything.
SUPPORTED_FILTERS = ("season", "week", "game_id", "team", "signal_type")


def _signal_matches(row: dict, params: dict) -> bool:
    if "game_id" in params and row.get("game_id") != params["game_id"]:
        return False
    if "team" in params and row.get("team") != params["team"]:
        return False
    return True


def _setup_telemetry(app) -> None:
    # Deferred: `weather.telemetry` loads only once OTel is actually on.
    from .telemetry import setup_telemetry

    setup_telemetry(app)


app = build_collector_app(
    CollectorDescriptor(
        name=COLLECTOR_NAME,
        cadence_class=CADENCE_CLASS,
        signal_types=SIGNAL_TYPES,
        supported_filters=SUPPORTED_FILTERS,
        capture=capture_week,
        signal_matches=_signal_matches,
        metrics=metrics,
        next_event_at=next_kickoff,
        setup_telemetry=_setup_telemetry,
    )
)


@app.get("/signals/convergence")
async def convergence(game_id: str = Query(...), season: int = 2026, week: int = 1):
    """weather's own extra route -- not part of the shared five. Reaches the
    lake via `app.state.collector_spec` rather than a module-level global."""
    spec = app.state.collector_spec
    series = build_convergence_series(spec.lake, spec.name, game_id, season, week)
    return {"game_id": game_id, "series": series, "count": len(series)}

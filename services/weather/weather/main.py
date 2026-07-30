"""weather's process wiring: the descriptor, `_signal_matches`, and
`/signals/convergence`. Everything else lives in `collector_core.app`.
"""

import asyncio

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
        # No `telemetry_module`: the fleet's shared wiring
        # (`collector_core.telemetry`) is the default, and weather needs
        # nothing beyond it. The forty near-identical lines this service used
        # to carry are gone.
    )
)


@app.get("/signals/convergence")
async def convergence(game_id: str = Query(...), season: int = 2026, week: int = 1):
    """weather's own extra route -- not part of the shared five. Reaches the
    lake via `app.state.collector_spec` rather than a module-level global.

    Offloaded whole rather than awaiting each lake call individually:
    `build_convergence_series` is a synchronous helper that does one
    `list_keys` plus one `read` per snapshot, so running it on the event loop
    would stall every other request -- including `/health` -- for the duration
    of a prefix scan. One `to_thread` moves all of it off, and the guarded
    lake makes forgetting an error rather than a stall.
    """
    spec = app.state.collector_spec
    series = await asyncio.to_thread(
        build_convergence_series, spec.lake, spec.name, game_id, season, week
    )
    return {"game_id": game_id, "series": series, "count": len(series)}

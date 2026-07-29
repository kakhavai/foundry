import os
from contextlib import asynccontextmanager
from datetime import timedelta

from collector_core.lake import build_lake_writer_from_env
from collector_core.refresh import RefreshGate
from collector_core.routes import CaptureState, CollectorSpec, build_collector_router
from fastapi import FastAPI, Query

from .auth import require_bearer_token
from .capture import CADENCE_CLASS, COLLECTOR_NAME, SIGNAL_TYPES, capture_week
from .metrics import metrics

# `player_id` is deliberately absent -- weather emits no players, and
# silently accepting it would return everything and read as a match.
SUPPORTED_FILTERS = ("season", "week", "game_id", "team", "signal_type")

REFRESH_FLOOR = timedelta(seconds=int(os.getenv("REFRESH_MIN_INTERVAL_SECONDS", "300")))


def _signal_matches(row: dict, params: dict) -> bool:
    """weather's row filter for every query parameter beyond season/week/signal_type."""
    if "game_id" in params and row.get("game_id") != params["game_id"]:
        return False
    if "team" in params and row.get("team") != params["team"]:
        return False
    return True


_state = CaptureState()
_refresh_gate = RefreshGate(REFRESH_FLOOR)
_lake = build_lake_writer_from_env()
_spec = CollectorSpec(
    name=COLLECTOR_NAME,
    cadence_class=CADENCE_CLASS,
    signal_types=SIGNAL_TYPES,
    supported_filters=SUPPORTED_FILTERS,
    capture=capture_week,
    state=_state,
    lake=_lake,
    metrics=metrics,
    refresh_gate=_refresh_gate,
    signal_matches=_signal_matches,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from .telemetry import setup_telemetry

        setup_telemetry(app)
    yield


app = FastAPI(lifespan=lifespan)
# A call, not a decorator, so auth.py never imports main -- one-way dependency.
app.middleware("http")(require_bearer_token)
app.include_router(build_collector_router(_spec))


@app.get("/signals/convergence")
async def convergence(game_id: str = Query(...), season: int = 2026, week: int = 1):
    """The ordered forecast series for one kickoff, with per-snapshot deltas.
    weather's own extra route, not part of the shared five -- derivable from
    the lake, but every consumer would otherwise reimplement it."""
    keys = _lake.list_keys(COLLECTOR_NAME, "venue_forecast_kickoff", season, week)
    series = []
    previous: dict | None = None
    for key in keys:
        body = _lake.read(key)
        if body["signal_type"] != "venue_forecast_kickoff":
            continue
        match = next((s for s in body["signals"] if s.get("game_id") == game_id), None)
        if match is None:
            continue
        entry = {
            "captured_at": body["captured_at"],
            "forecast_lead_hours": match.get("forecast_lead_hours"),
            "temperature_f": match.get("temperature_f"),
            "wind_speed_mph": match.get("wind_speed_mph"),
            "bands": match.get("bands"),
            "delta": None
            if previous is None
            else {
                "temperature_f": round(
                    match.get("temperature_f", 0) - previous.get("temperature_f", 0), 2
                ),
                "wind_speed_mph": round(
                    match.get("wind_speed_mph", 0) - previous.get("wind_speed_mph", 0),
                    2,
                ),
            },
        }
        series.append(entry)
        previous = match
    return {"game_id": game_id, "series": series, "count": len(series)}

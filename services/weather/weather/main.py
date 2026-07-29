import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import build_lake_writer_from_env
from collector_core.refresh import RefreshGate
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .auth import require_bearer_token
from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    CaptureState,
    capture_week,
)

# The filters this collector actually supports. `player_id` is deliberately
# absent — weather emits no players, and silently accepting it would return
# everything and read as a match. /catalog publishes this list so a consumer
# discovers the surface rather than guessing.
SUPPORTED_FILTERS = ("season", "week", "game_id", "team", "signal_type")

REFRESH_FLOOR = timedelta(seconds=int(os.getenv("REFRESH_MIN_INTERVAL_SECONDS", "300")))

_state = CaptureState()
_refresh_gate = RefreshGate(REFRESH_FLOOR)
_lake = build_lake_writer_from_env()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from .telemetry import setup_telemetry

        setup_telemetry(app)
    yield


app = FastAPI(lifespan=lifespan)
# Registered as a call rather than a decorator so auth.py never imports main —
# the dependency runs one way only.
app.middleware("http")(require_bearer_token)


def _rfc3339(value: datetime | None) -> str | None:
    return (
        None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/catalog")
async def catalog():
    """Self-description. The registry says a collector exists; this says what
    it currently offers."""
    return {
        "collector": COLLECTOR_NAME,
        "envelope_version": ENVELOPE_VERSION,
        "cadence_class": str(CADENCE_CLASS),
        "signal_types": list(SIGNAL_TYPES),
        "filters": list(SUPPORTED_FILTERS),
        "last_capture_at": _rfc3339(_state.last_capture_at),
        "coverage": {
            signal_type: envelope.coverage.to_dict()
            for signal_type, envelope in _state.envelopes.items()
        },
    }


def _filter_signals(envelope: Envelope, game_id: str | None, team: str | None) -> dict:
    body = envelope.to_dict()
    signals = body["signals"]
    if game_id is not None:
        signals = [s for s in signals if s.get("game_id") == game_id]
    if team is not None:
        signals = [s for s in signals if s.get("team") == team]
    body["signals"] = signals
    return body


@app.get("/signals")
async def signals(
    season: int | None = None,
    week: int | None = None,
    game_id: str | None = None,
    team: str | None = None,
    signal_type: str | None = None,
    player_id: str | None = Query(default=None),
):
    if player_id is not None:
        raise HTTPException(
            status_code=422,
            detail="weather emits no player_id; supported filters: "
            + ", ".join(SUPPORTED_FILTERS),
        )
    if signal_type is not None and signal_type not in SIGNAL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown signal_type {signal_type!r}; "
            f"expected one of {', '.join(SIGNAL_TYPES)}",
        )

    wanted = SIGNAL_TYPES if signal_type is None else (signal_type,)
    envelopes = []
    for name in wanted:
        envelope = _state.envelopes.get(name)
        if envelope is None:
            continue
        if season is not None and envelope.scope.get("season") != season:
            continue
        if week is not None and envelope.scope.get("week") != week:
            continue
        envelopes.append(_filter_signals(envelope, game_id, team))
    return {"envelopes": envelopes, "count": len(envelopes)}


@app.get("/signals/convergence")
async def convergence(game_id: str = Query(...), season: int = 2026, week: int = 1):
    """The ordered forecast series for one kickoff, with per-snapshot deltas.

    Derivable from the lake, but every consumer would otherwise reimplement it —
    and it is what makes the flat-band nowcast guard observable.
    """
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


@app.post("/refresh", status_code=202)
async def refresh(body: dict | None = None):
    """Force a capture outside the cadence, subject to the interval floor."""
    now = datetime.now(tz=UTC)
    refresh_id = _refresh_gate.try_acquire(now)
    if refresh_id is None:
        return JSONResponse(
            {"detail": "refresh requested too soon"},
            status_code=429,
            headers={"Retry-After": str(_refresh_gate.retry_after(now))},
        )

    scope = body or {}
    season = int(scope.get("season", 2026))
    week = int(scope.get("week", 1))
    async with httpx.AsyncClient(timeout=10.0) as client:
        envelopes = await capture_week(season, week, client=client, lake=_lake, now=now)
    _state.envelopes = envelopes
    _state.last_capture_at = now
    return {"refresh_id": refresh_id, "scope": {"season": season, "week": week}}

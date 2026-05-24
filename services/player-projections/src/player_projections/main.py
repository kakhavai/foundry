import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .client import fetch_projections

# In-memory projection cache — refreshed by the background polling loop.
_state: dict = {
    "projections": {},   # player_id → player dict
    "last_updated": None,
    "upstream_healthy": False,
}


async def _poll_loop() -> None:
    url = os.getenv("PLAYER_DATA_URL", "")
    api_key = os.getenv("PLAYER_DATA_API_KEY", "")
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))

    if not url:
        return  # stub mode — no upstream configured yet

    while True:
        try:
            players = await fetch_projections(url, api_key)
            _state["projections"] = {p["id"]: p for p in players}
            _state["last_updated"] = _now_iso()
            _state["upstream_healthy"] = True
        except Exception:
            _state["upstream_healthy"] = False

        await asyncio.sleep(interval)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from .telemetry import setup_telemetry
        setup_telemetry(app)

    task = asyncio.create_task(_poll_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/projections")
async def list_projections():
    return {
        "projections": list(_state["projections"].values()),
        "count": len(_state["projections"]),
        "last_updated": _state["last_updated"],
        "upstream_healthy": _state["upstream_healthy"],
    }


@app.get("/projections/{player_id}")
async def get_projection(player_id: str):
    player = _state["projections"].get(player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")
    return player

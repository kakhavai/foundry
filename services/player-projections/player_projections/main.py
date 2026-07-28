import asyncio
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .client import fetch_projections

# The three scoring modes the frontend offers. Kicker and DST rows repeat
# identically across all three; only skill-position values differ.
FORMATS = ("standard", "half-ppr", "ppr")

# Stored positions. FLEX is deliberately absent — it is a frontend display lane
# assembled from RB/WR/TE, so it is requested as `pos=RB,WR,TE`, not stored.
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")

DEFAULT_FORMAT = "ppr"


def _empty_cache() -> dict:
    return {"projections": [], "last_updated": None, "upstream_healthy": False}


# In-memory projection cache, one entry per scoring format — refreshed by the
# background polling loop. Each `projections` is a flat list in upstream order.
_state: dict = {fmt: _empty_cache() for fmt in FORMATS}


def _url_for(template: str, fmt: str) -> str:
    """Resolve the per-format document URL.

    `PLAYER_DATA_URL` carries a `{format}` placeholder — one config value for
    all three documents. A template without the placeholder resolves to the
    same URL for every format; that is caught by the `expect_format` check in
    `fetch_projections`, which fails the two formats it does not match rather
    than silently serving one document as all three.
    """
    return template.replace("{format}", fmt)


async def _poll_loop() -> None:
    template = os.getenv("PLAYER_DATA_URL", "")
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))

    if not template:
        return  # stub mode — no upstream configured yet

    while True:
        for fmt in FORMATS:
            # Each format is tracked independently: one document failing to
            # parse must not mark the other two unhealthy or drop their cache.
            try:
                players = await fetch_projections(
                    _url_for(template, fmt), expect_format=fmt
                )
                _state[fmt]["projections"] = players
                _state[fmt]["last_updated"] = _now_iso()
                _state[fmt]["upstream_healthy"] = True
            except Exception:
                _state[fmt]["upstream_healthy"] = False

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
async def list_projections(
    scoring: Literal["standard", "half-ppr", "ppr"] = Query(
        DEFAULT_FORMAT,
        alias="format",
        description="Scoring mode. Selects which cached document to serve.",
    ),
    pos: str | None = Query(
        None,
        description=(
            "Optional comma-separated position filter, e.g. `WR` or `RB,WR,TE` "
            "for the frontend's FLEX lane. Omitted returns every position."
        ),
    ),
):
    """Serve one scoring format's projections, optionally filtered by position.

    The filter is a convenience, not a boundary — the whole document is ~350
    rows, so a client is free to omit `pos` and slice client-side.
    """
    cached = _state[scoring]
    projections = cached["projections"]

    if pos is not None:
        wanted = [p.strip().upper() for p in pos.split(",") if p.strip()]
        unknown = [p for p in wanted if p not in POSITIONS]
        if unknown or not wanted:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown position(s): {unknown or ['(empty)']}. "
                    f"Valid positions are {list(POSITIONS)}. FLEX is a frontend "
                    f"lane — request it as pos=RB,WR,TE."
                ),
            )
        projections = [p for p in projections if p.get("pos") in wanted]

    return {
        "format": scoring,
        "projections": projections,
        "count": len(projections),
        "last_updated": cached["last_updated"],
        "upstream_healthy": cached["upstream_healthy"],
    }

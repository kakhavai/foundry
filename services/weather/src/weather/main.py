import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .client import fetch_weather_for_coords
from .stadiums import STADIUMS


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from .telemetry import setup_telemetry

        setup_telemetry(app)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/weather/stadiums")
async def all_stadiums_weather():
    async with httpx.AsyncClient(timeout=10.0) as client:
        results = []
        for stadium in STADIUMS.values():
            try:
                weather = await fetch_weather_for_coords(
                    stadium["latitude"], stadium["longitude"], client
                )
            except (httpx.HTTPStatusError, httpx.RequestError):
                weather = None
            results.append({**stadium, "weather": weather})
    return {"stadiums": results, "count": len(results)}


@app.get("/weather/stadiums/{stadium_id}")
async def stadium_weather(stadium_id: str):
    stadium = STADIUMS.get(stadium_id)
    if stadium is None:
        raise HTTPException(status_code=404, detail=f"Stadium not found: {stadium_id}")
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            weather = await fetch_weather_for_coords(
                stadium["latitude"], stadium["longitude"], client
            )
        except httpx.HTTPStatusError:
            raise HTTPException(status_code=502, detail="Weather API error")
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Weather API unreachable")
    return {**stadium, "weather": weather}

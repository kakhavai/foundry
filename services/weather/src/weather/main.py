import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .client import fetch_current_weather


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
async def stadiums():
    return {
        "status": "coming_soon",
        "detail": (
            "Stadium weather will return conditions for all NFL game sites"
            " for the current week."
        ),
    }


@app.get("/weather/{location}")
async def weather(location: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            data = await fetch_current_weather(location, client)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except httpx.HTTPStatusError:
            raise HTTPException(status_code=502, detail="Weather API error")
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Weather API unreachable")
    return data

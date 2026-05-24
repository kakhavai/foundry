import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .github import get_events, get_stats_data


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Only set up OTel when running in Kubernetes (env var injected by ConfigMap).
    # Tests run without it so there are no collector connection errors.
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


@app.get("/activity/{username}")
async def activity(username: str):
    async with httpx.AsyncClient() as client:
        try:
            events = await get_events(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return {"username": username, "events": events}


@app.get("/stats/{username}")
async def stats(username: str):
    async with httpx.AsyncClient() as client:
        try:
            data = await get_stats_data(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return data

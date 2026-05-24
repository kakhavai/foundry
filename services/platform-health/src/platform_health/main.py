import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


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


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": "platform-health"}


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

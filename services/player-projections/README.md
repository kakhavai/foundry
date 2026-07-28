# player-projections

Weekly fantasy football player projections. Polls S3 snapshots published by a projections generator that runs outside this repo, and serves the cached results. Runs in **stub mode** (empty projections) until that generator publishes.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check — `{"status": "ok"}` |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/projections` | All cached player projections |

In stub mode (no `PROJECTIONS_SNAPSHOT_URL` set), `/projections` returns `{"projections": [], "count": 0, "upstream_healthy": false}`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PROJECTIONS_SNAPSHOT_URL` | _(empty)_ | S3 URL of the projections JSON file; empty = stub mode |
| `POLL_INTERVAL_SECONDS` | `900` | How often to refresh from upstream (seconds) |

## Run locally

```bash
uv sync
uv run uvicorn player_projections.main:app --reload --host 0.0.0.0 --port 8001
```

## Test

```bash
uv run pytest -v
```

## Lint / format

```bash
uv run ruff check .
uv run ruff format .
```

## Docker

```bash
docker build -t player-projections:dev .
docker run -p 8001:8001 player-projections:dev
```

# github-stats

Minimal FastAPI service exposing GitHub activity data. Phase 1 — `/health` only.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Setup

```bash
uv sync
```

## Run locally (no Kubernetes)

```bash
uv sync         # install deps
uv run dev      # start with hot reload → http://localhost:8000
uv run test     # run tests
uv run lint     # lint with ruff
uv run format   # format with ruff
```

## Run in Kubernetes

See the [root README](../../README.md) for full stack setup. Quick deploy after a code change:

```bash
# From repo root
python scripts/deploy-local.py github-stats
# Then: kubectl port-forward svc/github-stats 8000:8000
```

Or bring the full stack up in one command:

```bash
python scripts/stack-up.py github-stats
```

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check — returns `{"status": "ok"}` |

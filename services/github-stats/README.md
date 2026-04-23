# github-stats

Minimal FastAPI service exposing GitHub activity data. Phase 1 — `/health` only.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Setup

```bash
uv sync
```

## Run Commands

| Command | Description |
|---|---|
| `uv run uvicorn github_stats.main:app --reload` | Start the service locally (hot reload) |
| `uv run pytest` | Run tests |
| `uv run pytest -v` | Run tests with verbose output |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Check formatting |
| `uv run ruff format .` | Fix formatting |

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check — returns `{"status": "ok"}` |

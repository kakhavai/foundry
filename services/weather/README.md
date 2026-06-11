# weather

Current conditions by NFL stadium location. Fetches live data from [Open-Meteo](https://open-meteo.com/) (no API key required).

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check — `{"status": "ok"}` |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/weather/stadiums` | Current conditions for all 30 NFL stadiums |
| `GET` | `/weather/stadiums/{id}` | Current conditions for a single stadium |

Stadium IDs are short slugs: `lambeau`, `gillette`, `metlife`, etc. Full list from `/weather/stadiums`.

## Run locally

```bash
uv sync
uv run uvicorn weather.main:app --reload --host 0.0.0.0 --port 8000
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
docker build -t weather:dev .
docker run -p 8000:8000 weather:dev
```

<!-- throwaway: verifying path-filtered integration test fires on services/** changes -->


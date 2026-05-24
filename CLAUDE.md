# Foundry — Claude Code Context

## What This Repo Is

Foundry is a Kubernetes platform monorepo. It is the long-term home for a **fantasy football prediction platform** — a frontend, backend, and a fully managed fleet of services all deployed and managed through a single, consistent infrastructure foundation.

The platform is built incrementally. Each phase proves a pattern, then the next phase adds a new service that reuses it.

---

## Long-Term Vision: Fantasy Football Platform

The end-state is a fully managed fantasy football product running on Foundry:

```
player-data (internal, auth-gated)
  └── hidden backend — aggregates data from proprietary sources
      (injury reports, weather, news, betting lines, field type, etc.)
  └── never exposed publicly; accessed only by internal services via API key
  └── the methodology and data pipeline stay private

player-projections (this service — first real consumer of player-data)
  └── polls player-data, exposes weekly player projections via API

injury-tracker (future internal consumer)
  └── feeds into player-data

fantasy-frontend (future)
  └── user-facing web UI consuming the projections API
```

The data collection services (injury, weather, news, betting lines, field type, and others) are all internal inputs to `player-data` — they are not separate user-facing services. `player-data` is the single gatekeeper for that data.

---

## Current Services

| Service | Port | Status | Purpose |
|---|---|---|---|
| `github-stats` | 8000 | Live | GitHub activity API — proves the platform pattern |
| `player-projections` | 8001 | Stub mode | Polls `player-data` for weekly projections; returns empty until `player-data` is built |

### player-projections — How It Works Now

Runs in **stub mode** when `PLAYER_DATA_URL` is empty (no upstream yet). Returns `{"projections":[], "count":0, "upstream_healthy":false}`. Once `player-data` is deployed:

1. Set `PLAYER_DATA_URL` in the ConfigMap
2. Create a Kubernetes Secret `player-data-credentials` with key `api-key`
3. Service begins polling every 15 minutes (configurable via `POLL_INTERVAL_SECONDS`)

The response shape from `player-data` is to be defined when that service is built. The client in `services/player-projections/src/player_projections/client.py` expects `{"players": [...]}` at minimum — each player object needs at least an `id` field for the cache key.

---

## Repo Layout

```
services/<name>/        Python service (FastAPI, uv, pyproject.toml)
helm/charts/generic-service/    Base Helm chart all services share
helm/values/<name>/     Per-service value overrides
.github/actions/        Composite CI actions: python-lint, python-test, helm-lint, build-push
.github/workflows/      Per-service CI callers (one file per service)
infra/grafana-stack/    Helmfile: OTel Collector, Prometheus, Loki, Tempo, Grafana
infra/kind/             Kind cluster config for local dev
scripts/                deploy-local.py, stack-up.py
docs/                   Architecture, onboarding, service contract
```

---

## How CI Works

Each service has one workflow file (`.github/workflows/<service>.yml`) that calls shared composite actions directly. Four parallel/sequential jobs per PR:

- `lint` → `.github/actions/python-lint` (ruff check + format)
- `test` → `.github/actions/python-test` (pytest)
- `helm-lint` → `.github/actions/helm-lint`
- `build-push` → needs all three; runs only on push to main

---

## Adding a New Service

See `docs/onboarding.md`. Short version:
1. `services/<name>/` — FastAPI app, Dockerfile, pyproject.toml, uv.lock
2. `helm/values/<name>/values.yaml`
3. Copy `.github/workflows/player-projections.yml` → `.github/workflows/<name>.yml`
4. Register in `scripts/deploy-local.py` and `scripts/stack-up.py`

If the service needs secrets: add `extraEnv` to the values file (see `helm/values/player-projections/values.yaml` for the pattern).

---

## Dockerfile Pattern (Canonical)

All services use uv's official multi-stage pattern for packaged services:

```dockerfile
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Step 1: deps only (layer cached until uv.lock/pyproject.toml change)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
# Step 2: install package as wheel (--no-editable bakes it into the venv)
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# No PYTHONPATH needed — --no-editable installs the package as a proper wheel
```

---

## OTel Pattern

OTel activates only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (injected by the generic-service Helm chart via ConfigMap). Services run and test cleanly without a collector.

```python
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from .telemetry import setup_telemetry
    setup_telemetry(app)
```

Each service has a `telemetry.py` with traces (OTLP gRPC → OTel Collector → Tempo) and metrics (`PrometheusMetricReader` → `/metrics` → Prometheus).

---

## Helm Chart — Secret Support

Services that need Kubernetes Secrets use `extraEnv` in their values file:

```yaml
extraEnv:
  - name: MY_SECRET_VALUE
    valueFrom:
      secretKeyRef:
        name: my-k8s-secret
        key: the-key
        optional: true   # allows pod to start before secret exists
```

The `optional: true` flag means a pod deploys even when the Secret hasn't been created yet — important for stub-mode services like `player-projections` before `player-data` is live.

---

## Testing

```bash
cd services/<name>
uv run pytest -v
```

Tests use `respx` to mock `httpx` calls. OTel not initialized in tests. State-based endpoint tests pre-populate the in-memory cache via `_state` directly.

---

## Local Stack

```bash
python scripts/stack-up.py                    # Kind cluster + all services + port-forwards
python scripts/deploy-local.py <name>         # Redeploy a single service
```

Requires: `kind`, `kubectl`, `helm`, `helmfile`, `docker`.

---

## Key Decisions

- **Split lint/test CI jobs** — parallel jobs catch failures independently, standard since ~2023.
- **`--no-editable` Docker** — canonical uv pattern for packages; wheels go into venv, no `src/` copy or PYTHONPATH in runtime.
- **Per-service workflow files** — one file to copy per onboard, no reusable workflow indirection.
- **OTel guard on env var** — no collector needed for local dev or tests; Kubernetes injects it.
- **`player-data` internal-only** — proprietary data pipeline never exposed publicly; only `player-projections` (and future internal consumers) can reach it via authenticated polling.
- **Stub mode for not-yet-built upstreams** — `PLAYER_DATA_URL` empty = service runs, returns empty data, no crashes. Lets the service be deployed and observed before its dependency exists.

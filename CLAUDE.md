# Foundry — Claude Code Context

## What This Repo Is

Foundry is a Kubernetes platform monorepo demonstrating a "golden path" for Python microservices: consistent CI, Helm deployment, and OTel observability that any new service inherits by following the onboarding guide.

## Repo Layout

```
services/<name>/        Python service (FastAPI, uv, pyproject.toml)
helm/charts/generic-service/    Base Helm chart all services use
helm/values/<name>/     Per-service value overrides
.github/actions/        Composite CI actions (python-lint, python-test, helm-lint, build-push)
.github/workflows/      Per-service workflow callers
infra/grafana-stack/    Helmfile: OTel Collector, Prometheus, Loki, Tempo, Grafana
infra/kind/             Kind cluster config for local dev
scripts/                deploy-local.py, stack-up.py
docs/                   Architecture, onboarding, service contract
```

## Services

| Service | Port | Purpose |
|---|---|---|
| `github-stats` | 8000 | Fetches GitHub user activity/stats via GitHub API |
| `platform-health` | 8001 | Proves the golden path works for a second service |

## How CI Works

Each service has its own workflow file (`.github/workflows/<service>.yml`) that calls shared composite actions directly — no reusable workflow indirection. Four jobs run per PR:

- `lint` → `.github/actions/python-lint` (ruff check + format)
- `test` → `.github/actions/python-test` (pytest)
- `helm-lint` → `.github/actions/helm-lint`
- `build-push` → needs all three; only runs on push to main

## Adding a New Service

Follow `docs/onboarding.md`. The short version:
1. Create `services/<name>/` with FastAPI app, Dockerfile, pyproject.toml, uv.lock
2. Add `helm/values/<name>/values.yaml`
3. Copy `.github/workflows/platform-health.yml` → `.github/workflows/<name>.yml`, update service name and port
4. Register in `scripts/deploy-local.py` and `scripts/stack-up.py`

Required endpoints every service must expose: `GET /health` → `{"status":"ok"}`, `GET /metrics` → Prometheus text format.

## Dockerfile Pattern

All services use the canonical uv multi-stage pattern:

```dockerfile
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Step 1: deps only (cached layer)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
# Step 2: install package as wheel (not editable)
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
# copy only the venv — package code is inside the wheel, src/ not needed at runtime
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
```

**No PYTHONPATH needed.** `--no-editable` installs the package as a proper wheel inside the venv so the module is importable without any env var tricks.

## OTel Pattern

OTel is **only activated when `OTEL_EXPORTER_OTLP_ENDPOINT` is set** (injected by the `generic-service` Helm chart via ConfigMap). This means services run and test cleanly locally without a collector. The guard lives in `main.py`'s lifespan:

```python
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from .telemetry import setup_telemetry
    setup_telemetry(app)
```

Each service has a `telemetry.py` that sets up traces (OTLP gRPC → OTel Collector → Tempo) and metrics (`PrometheusMetricReader` → `/metrics` endpoint → Prometheus).

## Testing

```bash
cd services/<name>
uv run pytest -v
```

Tests use `respx` to mock `httpx` calls — no real network calls in CI. OTel is not initialized in tests (no `OTEL_EXPORTER_OTLP_ENDPOINT` set).

## Local Stack

```bash
python scripts/stack-up.py          # Kind cluster + all services + port-forwards
python scripts/deploy-local.py <name>   # Redeploy a single service
```

Requires: `kind`, `kubectl`, `helm`, `helmfile`, `docker`.

## Key Decisions Made This Session (2026-05-23)

- **Split lint/test CI jobs**: Industry standard since ~2023 — parallel jobs catch failures independently and give faster feedback than a combined job.
- **`--no-editable` in Docker**: The canonical uv pattern for packaged services. Builds the wheel into the venv so the runtime image only needs `.venv` copied, not `src/`. No PYTHONPATH.
- **Per-service workflow files**: Simpler than a shared reusable workflow. Each service's CI is a thin caller that invokes composite actions — one file to copy when onboarding.
- **OTel guard on env var**: Avoids collector connection errors in local dev and tests. Kubernetes injects the env var via ConfigMap.

# Onboarding a New Service

This guide walks through adding a new Python HTTP service to Foundry. The result: CI runs on every push, images are built and pushed to GHCR on merge to `main`, Helm deploys the service to the Kind cluster, and Grafana shows logs, traces, and metrics — with no observability config in the service.

---

## Prerequisites

- Docker running locally
- Kind cluster running (`kind create cluster --config infra/kind/cluster.yaml`)
- Observability stack deployed (`cd infra/grafana-stack && helmfile apply`)

---

## Step 1: Create the service directory

```bash
mkdir -p services/<service-name>/<service_name>
mkdir services/<service-name>/tests
```

Your service needs these files (see `services/player-projections/` as the reference):

```
services/<service-name>/
├── pyproject.toml
├── Dockerfile
├── <service_name>/
│   ├── __init__.py
│   └── main.py        # FastAPI app
└── tests/
    └── test_health.py
```

The service must satisfy the [service contract](service-contract.md):
- `GET /health` → `{"status": "ok"}`
- `GET /metrics` → Prometheus-format metrics
- OTel initialization guarded on `OTEL_EXPORTER_OTLP_ENDPOINT`

**Collectors build from the repo root**, not the service directory, because
they depend on the `libs/collector-core/` workspace member by path:

    docker build -f services/<name>/Dockerfile -t <name>:local .

The build stage copies `libs/collector-core/` before `uv sync`, since the lock
cannot resolve without the member present. Services that do not consume the
shared library keep the original service-directory context. See
`services/weather/` for the reference collector.

---

## Step 2: Add Helm values

Create `helm/values/<service-name>/values.yaml` with the service-specific values. Copy from `helm/values/weather/values.yaml` and change the name and port:

```yaml
service:
  name: <service-name>
  port: <port>           # pick a port not used by another service

image:
  repository: ghcr.io/kakhavai/foundry/<service-name>

containerPort: <port>

resources:
  limits:
    cpu: 250m
    memory: 256Mi
  requests:
    cpu: 100m
    memory: 128Mi

otel:
  resourceAttributes: "service.name=<service-name>,service.namespace=foundry"
```

Verify the chart lints cleanly:

```bash
helm lint helm/charts/generic-service -f helm/values/<service-name>/values.yaml
```

---

## Step 3: Add CI workflow

Copy `.github/workflows/player-projections.yml` to `.github/workflows/<service-name>.yml` and replace all occurrences of `player-projections` with your service name.

No CI logic lives in this file — it calls the shared composite actions.

---

## Step 4: Deploy locally

```bash
python scripts/deploy-local.py <service-name>
```

Or bring up the full stack including your service:

```bash
python scripts/stack-up.py <service-name>
```

---

## Step 5: Verify

```bash
# Service health
curl http://localhost:<port>/health

# Prometheus metrics
curl http://localhost:<port>/metrics

# Grafana — http://localhost:3000 (admin / admin)
# Traces and logs appear once the service handles requests
```

---

## What you get automatically

- **CI:** lint, test, Helm lint on every push; image pushed to GHCR on merge to `main`
- **Kubernetes:** Deployment + ClusterIP Service + ConfigMap with OTel env vars
- **Prometheus:** auto-discovered via pod annotations (`prometheus.io/scrape: "true"`)
- **OTel traces:** exported to the cluster-level Collector → Tempo
- **Logs:** collected by Loki from stdout

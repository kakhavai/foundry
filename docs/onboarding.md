# Onboarding a New Service

This guide walks through adding a new Python HTTP service to Foundry. The result: CI runs on every push, images are built and pushed to GHCR on merge to `main`, Helm deploys the service to the Kind cluster, and Grafana shows logs, traces, and metrics — with no observability config in the service.

> **Adding a collector? Do not follow this guide by hand.** Run
> `scripts/new-collector.py`, which generates every file below plus the capture
> surface, and read [`collectors.md`](collectors.md). This guide is for a
> service that is *not* a collector — there is one today,
> `player-projections`.

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

## Step 3: CI — nothing to do

There is no per-service workflow file to copy. `.github/workflows/services.yml`
covers every deployable service as a matrix leg, and the matrix is computed from
`contracts/collector-registry.yaml` plus each service's Helm values by
`.github/actions/changed-services`. Creating `helm/values/<service-name>/`
in Step 2 (and, for a collector, the registry entry) is the registration.

That includes the path filtering: `changed-services` generates a
`dorny/paths-filter` rule per service, so your service's jobs still run only
when your service's files — or something it shares, like
`helm/charts/generic-service/` or `libs/` — actually changed.

Confirm your service is picked up before pushing:

```bash
uv run --no-project --with pyyaml==6.0.3 python3 \
  .github/actions/changed-services/filters.py --emit entries
```

Your service should appear as a key. If it does not, the most likely cause is a
missing or malformed `helm/values/<service-name>/values.yaml` — that is where
the tooling reads a service's port and Secret names from.

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

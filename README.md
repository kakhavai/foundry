# Foundry

A standardized Kubernetes service delivery platform — CI/CD, Helm-based deployment, GitOps, and integrated observability via OpenTelemetry and the Grafana LGTM stack.

```mermaid
graph TD
    Dev["Developer"]

    subgraph "CI — GitHub Actions"
        Lint["Lint + Test"]
        Build["Build & Push Image"]
    end

    subgraph "Registry"
        GHCR["GHCR"]
    end

    subgraph "GitOps"
        GitOpsRepo["infra/gitops"]
        Argo["Argo CD"]
    end

    subgraph "Kubernetes"
        Services["Services"]
        OTelCol["OTel Collector"]
        Grafana["Grafana\n(Loki · Tempo · Prometheus)"]
    end

    Assistant["Incident Assistant\n(Claude API)"]

    Dev -->|"git push"| Lint --> Build --> GHCR
    Build -->|"update image tag"| GitOpsRepo --> Argo --> Services
    Services -->|"OTLP"| OTelCol --> Grafana
    Grafana --> Assistant
```

---

## Repo Structure

```
foundry/
  services/              # service source code
  helm/
    charts/              # generic-service Helm chart (shared by all services)
    values/              # per-service value overrides
  scripts/               # local dev/deploy helper scripts
  .github/workflows/     # CI pipelines (per-service, using generic-service chart)
  infra/
    kind/                # local Kind cluster config
    grafana-stack/       # observability stack manifests
    gitops/              # Argo CD source of truth for deploys
  docs/
    architecture/        # system diagrams and component docs
    plans/               # design docs and implementation plans
    runbooks/            # operational runbooks
```

---

## Phases

| Phase | Dates | Goal |
|---|---|---|
| 1 | Apr 13 – Apr 26 | First paved road — one service, full stack |
| 2 | Apr 27 – May 10 | Golden path — reusable conventions, second service |
| 3 | May 11 – May 31 | GitOps + safe deployment, rollback, release observability |
| 4 | Jun 1 – Jun 14 | AI-assisted incident triage |

---

## Local Dev + Deploy

### Prerequisites

| Tool | macOS | Windows |
|---|---|---|
| Docker | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| uv | `brew install uv` | `winget install astral-sh.uv` |
| kind | `brew install kind` | `winget install Kubernetes.kind` |
| kubectl | `brew install kubectl` | `winget install Kubernetes.kubectl` |
| helm | `brew install helm` | `winget install Helm.Helm` |
| helmfile | `brew install helmfile` | `scoop install helmfile` |
| helm-diff | `helm plugin install https://github.com/databus23/helm-diff` | `helm plugin install https://github.com/databus23/helm-diff` |

> After installing with winget/scoop on Windows, open a new terminal for PATH changes to take effect.
>
> **Windows:** `helm plugin install` requires PowerShell Core. Install it with `winget install Microsoft.PowerShell` and open a new terminal before running the plugin install.

### Run a service locally (no Kubernetes)

```bash
cd services/github-stats
uv sync        # install deps into .venv
uv run dev     # start with hot reload (http://localhost:8000)
uv run test    # run tests
uv run lint    # lint with ruff
uv run format  # format with ruff
```

### Spin up the full local stack

One command from the repo root brings up the cluster, observability, all services, and all port-forwards:

```bash
python scripts/stack-up.py
```

Or pick specific services:

```bash
python scripts/stack-up.py github-stats
```

Once running, access everything at:

| Service | URL |
|---|---|
| github-stats | http://localhost:8000 |
| Grafana | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |
| Loki | http://localhost:3100/ready |
| Tempo | http://localhost:3200/ready |

Ctrl+C stops the port-forwards. The cluster and Helm releases stay running so you can restart forwards without re-deploying. To fully tear down:

```bash
kind delete cluster --name foundry
```

### Deploy a single service (without full stack)

```bash
# From repo root
python scripts/deploy-local.py github-stats
```

This runs:
1. `docker build -t github-stats:local services/github-stats/`
2. `kind load docker-image github-stats:local --name foundry`
3. `helm upgrade --install github-stats helm/charts/generic-service -f helm/values/github-stats/values.yaml ...`

### Local Kubernetes cluster (Kind)

```bash
# Create the cluster manually if needed
kind create cluster --config infra/kind/cluster.yaml

# Verify it's up
kubectl get nodes

# Tear down
kind delete cluster --name foundry
```

### Observability Stack

Deploys OTel Collector, Loki, Tempo, Prometheus, and Grafana into the `monitoring` namespace via Helmfile.

```bash
# Add chart repos (first time only)
cd infra/grafana-stack
helmfile repos

# Deploy the full stack
helmfile apply

# Verify all pods are running
kubectl get pods -n monitoring

# Tear down
helmfile destroy
```

**Access the UIs:**

```bash
# Grafana — http://localhost:3000 (login: admin / admin)
kubectl port-forward -n monitoring svc/grafana 3000:80

# Prometheus — http://localhost:9090
kubectl port-forward -n monitoring svc/prometheus-server 9090:80

# Loki (raw API) — http://localhost:3100/ready
kubectl port-forward -n monitoring svc/loki 3100:3100

# Tempo (raw API) — http://localhost:3200/ready
kubectl port-forward -n monitoring svc/tempo 3200:3200
```

The `github-stats` dashboard loads automatically in Grafana. Panels show live data once the service is running and instrumented with the OTel SDK.

---

## Docs

- [Architecture Overview](docs/architecture/architecture-overview.md)
- [Why This Design](docs/why-this-design.md)
- [Phase 1 — First Paved Road](docs/architecture/phase-1-first-paved-road.md)
- [Phase 2 — Golden Path](docs/architecture/phase-2-golden-path.md)
- [Phase 3 — GitOps Deployment](docs/architecture/phase-3-gitops-deployment.md)
- [Phase 4 — Incident Assistant](docs/architecture/phase-4-incident-assistant.md)

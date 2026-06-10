# Foundry

A production-grade Kubernetes platform monorepo covering the full service delivery lifecycle: CI/CD, Helm-based deployment, GitOps with Argo CD, and integrated observability via OpenTelemetry and the Grafana LGTM stack (Loki, Grafana, Tempo, Prometheus).

The application running on this platform is a fantasy football prediction product. The platform is application-agnostic; the fantasy app provides a real-world workload that exercises the platform end-to-end.

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

| Phase | Goal |
|---|---|
| 1 | First paved road — one service, full stack |
| 2 | Golden path — reusable conventions, second service |
| 3 | GitOps + safe deployment, rollback, release observability |
| 4 | AI-assisted incident triage |
| 5 | Resilience testing + AI agent adversarial layer |
| 6 | AWS deployment — EKS via Terraform, ECR, ALB ingress, IRSA + OIDC |

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
cd services/weather
uv sync                                                                    # install deps into .venv
uv run uvicorn weather.main:app --reload --host 0.0.0.0 --port 8000       # start with hot reload
uv run pytest                                                              # run tests
uv run ruff check .                                                        # lint
uv run ruff format .                                                       # format
```

```bash
# Same pattern for other services — adjust the module name and port:
cd services/player-projections
uv sync
uv run uvicorn player_projections.main:app --reload --host 0.0.0.0 --port 8001
uv run pytest
uv run ruff check .
uv run ruff format .
```

### Spin up the full local stack

One command from the repo root brings up the cluster, observability, all services, and all port-forwards:

```bash
python scripts/stack-up.py
```

Or pick specific services:

```bash
python scripts/stack-up.py weather
```

Once running, access everything at:

| Service | URL |
|---|---|
| weather | http://localhost:8000 |
| player-projections | http://localhost:8001 |
| Grafana | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |
| Loki | http://localhost:3100/ready |
| Tempo | http://localhost:3200/ready |
| Argo CD | http://localhost:8080 (admin / printed on startup) |

Ctrl+C stops the port-forwards. The cluster and Helm releases stay running so you can restart forwards without re-deploying. To fully tear down:

```bash
kind delete cluster --name foundry
```

### Deploy a single service (without full stack)

```bash
# From repo root
python scripts/deploy-local.py weather
```

This runs:
1. `docker build -t weather:local services/weather/`
2. `kind load docker-image weather:local --name foundry`
3. `helm upgrade --install weather helm/charts/generic-service -f helm/values/weather/values.yaml ...`

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

The `weather` and `player-projections` dashboards load automatically in Grafana. Panels show live data once services are running and instrumented with the OTel SDK.

### Argo CD

`stack-up.py` installs Argo CD automatically — you don't need to run these steps manually unless you're setting up the cluster without the script.

**What `stack-up.py` does with Argo CD:**
1. Installs Argo CD into the `argocd` namespace via Helmfile (`infra/argo/`)
2. Waits for the server to be ready
3. Applies `infra/gitops/argo/app-of-apps.yaml` — this single manifest creates one Argo CD Application per service
4. Port-forwards the UI to `http://localhost:8080` and prints the admin password

**If you need to set it up manually:**

```bash
# Install Argo CD
cd infra/argo
helmfile repos
helmfile apply

# Wait for the server
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=180s

# Bootstrap the app-of-apps (creates one Application per service)
kubectl apply -f infra/gitops/argo/app-of-apps.yaml

# Port-forward the UI
kubectl port-forward -n argocd svc/argocd-server 8080:80

# Get the admin password
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath="{.data.password}" | base64 -d
```

**What Argo CD is doing:**

Argo CD watches `https://github.com/kakhavai/foundry` (the real GitHub repo) every ~3 minutes. When CI merges a change to `main` and commits a new image tag to `infra/gitops/envs/local/<service>/values.yaml`, Argo CD detects it and runs a Helm upgrade on your local cluster automatically. You never run `helm upgrade` for a production deploy — you commit to Git and Argo CD reconciles.

To trigger a deploy manually (e.g. for rollback), edit the tag file and push to `main`:

```bash
# Or use the rollback script:
python scripts/rollback.py weather <target-tag>
```

See [docs/deployment-lifecycle.md](docs/deployment-lifecycle.md) for the full deploy flow.

---

## Docs

- [Architecture Overview](docs/architecture/architecture-overview.md)
- [Why This Design](docs/why-this-design.md)
- [Phase 1 — First Paved Road](docs/architecture/phase-1-first-paved-road.md)
- [Phase 2 — Golden Path](docs/architecture/phase-2-golden-path.md)
- [Phase 3 — GitOps Deployment](docs/architecture/phase-3-gitops-deployment.md)
- [Phase 4 — Incident Assistant](docs/architecture/phase-4-incident-assistant.md)
- [Phase 5 — Resilience Testing & AI Adversarial Layer](docs/architecture/phase-5-resilience-and-ai-testing.md)
- [Phase 6 — AWS Deployment](docs/architecture/phase-6-aws-deployment.md)

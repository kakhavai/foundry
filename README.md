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
        GitOps["infra/gitops"]
        Argo["Argo CD"]
    end

    subgraph "Kubernetes"
        Services["Services"]
        OTelCol["OTel Collector"]
        Grafana["Grafana\n(Loki · Tempo · Prometheus)"]
    end

    Assistant["Incident Assistant\n(Claude API)"]

    Dev -->|"git push"| Lint --> Build --> GHCR
    Build -->|"update image tag"| GitOps --> Argo --> Services
    Services -->|"OTLP"| OTelCol --> Grafana
    Grafana --> Assistant
```

---

## Repo Structure

```
foundry/
  services/              # service source code
  helm/charts/           # Helm charts per service
  .github/workflows/     # CI pipelines (reusable + per-service)
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

> Setup instructions will be added at the end of Phase 1.

---

## Docs

- [Master Architecture](docs/architecture/master-architecture.md)
- [Why This Design](docs/why-this-design.md)
- [Phase 1 — First Paved Road](docs/architecture/phase-1-first-paved-road.md)
- [Phase 2 — Golden Path](docs/architecture/phase-2-golden-path.md)
- [Phase 3 — GitOps Deployment](docs/architecture/phase-3-gitops-deployment.md)
- [Phase 4 — Incident Assistant](docs/architecture/phase-4-incident-assistant.md)

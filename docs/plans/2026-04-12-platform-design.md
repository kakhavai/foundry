# Platform Design — Foundry

**Date:** 2026-04-12
**Status:** Approved

---

## Summary

Foundry is a monorepo-based Kubernetes service delivery platform. It establishes a standardized golden path for building, deploying, and operating Python services on Kubernetes — with CI/CD, Helm-based deployment, GitOps, and integrated observability via OpenTelemetry and the Grafana LGTM stack.

---

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Repo structure | Monorepo (`kakhavai/foundry`) | Single portfolio artifact; scales to multi-service with clear directory conventions |
| Service language | Python | Familiar, fast to scaffold, good OTel SDK |
| First service | `github-stats` — GitHub Activity Stats API | Real HTTP calls → meaningful traces; self-referential to the project |
| Diagram format | Mermaid in markdown | Renders natively in GitHub; version-controllable; always current |
| Packaging | Helm | Most widely understood K8s packaging format; explicit values contract |
| Deployment model | GitOps via Argo CD | Auditable deploys; git revert = rollback; cluster drift prevention |
| Instrumentation | OpenTelemetry SDK | Vendor-neutral; one instrumentation, swappable backends |
| Telemetry backend | Grafana LGTM (Loki + Tempo + Prometheus + Grafana) | Open source; self-hostable; OTel-native; local dev parity |
| Collector topology | Shared OTel Collector (not sidecars) | Avoids linear resource multiplication; collector is a platform concern |
| AI layer | Assistive triage CLI (Claude API) | Reduces MTTR; no autonomous action; correct scope for system maturity |

---

## Repo Structure

```
foundry/
  services/
    github-stats/        # Python HTTP API — GitHub Activity Stats
  helm/
    charts/              # Helm charts per service
  .github/
    workflows/           # CI pipelines (reusable + per-service)
  infra/
    kind/                # Local Kind cluster config
    grafana-stack/       # Loki, Tempo, Prometheus, Grafana manifests
    gitops/              # GitOps deploy manifests (Argo CD source of truth)
  docs/
    architecture/        # System diagrams and architecture docs
    plans/               # Design docs and implementation plans
    runbooks/            # Operational runbooks
  README.md
```

---

## Phase Plan

| Phase | Dates | Focus |
|---|---|---|
| 1 | Apr 13 – Apr 26 | First paved road — one service, full stack |
| 2 | Apr 27 – May 10 | Golden path — reusable, second service |
| 3 | May 11 – May 31 | GitOps + safe deployment |
| 4 | Jun 1 – Jun 14 | AI incident assistant |

---

## Architecture

See [Master Architecture](../architecture/master-architecture.md).

## Why This Design

See [Why This Design](../why-this-design.md).

# Phase 2 — Golden Path

**Dates:** April 27 – May 10, 2026

**Goal:** Prove this is a reusable platform path, not a one-service demo. Onboard a second service using the same conventions. Extract and standardize shared patterns.

---

## Diagram

```mermaid
graph TD
    subgraph "Reusable CI"
        SharedWF["Shared GitHub Actions\nWorkflow Templates\n(.github/workflows/shared-*)"]
    end

    subgraph "Services"
        SvcA["github-stats\n(Python)"]
        SvcB["second-service\n(Python)"]
    end

    subgraph "Shared Platform Conventions"
        HelmLib["Common Helm Chart Pattern\n(base values + overrides)"]
        OTelStd["Standard OTel Config\n(collector endpoint, resource attrs)"]
        DashTpl["Dashboard Template\n(per-service parameterized)"]
    end

    SharedWF --> SvcA & SvcB
    HelmLib --> SvcA & SvcB
    OTelStd --> SvcA & SvcB
    DashTpl --> SvcA & SvcB
```

---

## What Gets Built

### Second Service
A second Python service onboarded through the same path as `github-stats`. Candidates:
- A small internal utility API (e.g., a health aggregator that polls other services)
- A simple async worker

The second service exists primarily to prove the pattern works for more than one team/service — not for its own functionality.

### Reusable CI Workflow Templates
Common CI steps extracted into reusable GitHub Actions workflows under `.github/workflows/shared-*.yml`:
- `shared-lint-test.yml` — parameterized lint and test job
- `shared-build-push.yml` — parameterized build and push job
- `shared-helm-lint.yml` — Helm validation job

Each service's workflow file becomes a thin caller that passes service-specific parameters.

### Standardized Config Conventions
A documented contract for what any Foundry service must provide:
- Required environment variables (OTel endpoint, service name, service version)
- Required labels on Kubernetes resources (`app.kubernetes.io/name`, `app.kubernetes.io/version`, etc.)
- Required Helm values structure
- Required health endpoint (`GET /health`)

### Standard Telemetry Setup
A shared Python package or copy-paste module (`foundry_telemetry`) that any service imports to get:
- OTel SDK initialization (traces + metrics + logs)
- Standard resource attributes (service name, version, environment)
- Prometheus metrics endpoint wiring

### Dashboard Template
A parameterized Grafana dashboard (JSON template with `${service_name}` variables) that generates a working starter dashboard for any onboarded service.

---

## Milestones

| Date | Checkpoint |
|---|---|
| May 3 | Second service started, shared CI workflow partly extracted |
| May 10 | Two services onboarded, onboarding documentation complete, golden path clearly reusable |

---

## Deliverables

- `services/<second-service>/` — second working service
- `.github/workflows/shared-*.yml` — reusable CI templates
- `docs/onboarding.md` — "How to onboard a new service"
- `docs/service-contract.md` — required structure and conventions
- `infra/grafana-stack/dashboards/service-template.json` — parameterized dashboard template

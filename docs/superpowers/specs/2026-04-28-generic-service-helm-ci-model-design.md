# Design: Generic-Service Helm Chart + CI Caller Model

**Date:** 2026-04-28
**Status:** Approved

---

## Summary

Refactor the Helm and CI model so that adding a new service to Foundry requires copying two files — a CI caller workflow and a Helm values file — and nothing else. CI runs, Helm deploys, Grafana shows it automatically.

This work completes Phase 1 and establishes the base for Phase 2's golden path demonstration.

---

## Context

The existing `helm/charts/github-stats/` is a per-service chart. The existing `ci.yml` has hardcoded job names for github-stats. Both work for one service but don't scale — a second service would require duplicating chart templates and CI logic.

The composite actions (`python-lint-test`, `helm-lint`) already exist and are the right abstraction. This design completes the pattern.

---

## Design

### Helm

**Base chart:** `helm/charts/generic-service/`

One parameterized chart used by every standard HTTP service. Contains:
- `Deployment` — replicas, image, resource limits all from values
- `Service` (ClusterIP)
- `ConfigMap` for environment config

Baked in by default (no per-service config required):
- Env vars: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`
- Pod annotations: `prometheus.io/scrape: "true"`, `prometheus.io/port`

Services that don't fit this shape (StatefulSet, CronJob, custom sidecars) get their own chart. The generic-service chart is for standard HTTP services only.

**Per-service values:** `helm/values/<service-name>/values.yaml`

Contains everything service-specific: image name, service port, resource limits, replica count. The OTel env vars are already wired by the chart — no observability config needed in the values file.

**Argo CD (Phase 3):** An ApplicationSet scans `helm/values/*/values.yaml` and auto-generates an Argo CD Application per directory. Dropping in a values file is sufficient to get a deployed service.

### CI

**Caller template:** `.github/workflows/_service-template.yml`

A thin reusable workflow (~15 lines). Takes a service name, sets path filters on `services/<service>/**` and `helm/values/<service>/**`, and delegates to:
- `.github/actions/python-lint-test` (exists)
- `.github/actions/helm-lint` (exists)
- `.github/actions/build-push` (to be built as Phase 1 completion work)

**Per-service caller:** `.github/workflows/<service-name>.yml`

Copy of the template with the service name filled in. This is the one file a developer touches to get CI.

**github-stats migration:** `ci.yml` is replaced by `.github/workflows/github-stats.yml` using the caller pattern.

### Observability

Every service deployed via `generic-service` gets full LGTM observability with zero per-service config:
- **Logs** — stdout collected by Loki automatically (node-level collection)
- **Traces** — service exports OTLP to the OTel Collector via the injected endpoint env var
- **Metrics** — Prometheus auto-discovers the service via pod annotations

The service must be instrumented with the OTel SDK. The platform handles routing and storage.

---

## Doc Updates

Four docs updated to reflect this model:

| File | What Changes |
|---|---|
| `phase-1-first-paved-road.md` | CI: thin caller + composite actions; Helm: generic-service + values/github-stats/ |
| `phase-2-golden-path.md` | Remove shared-*.yml section; remove foundry_telemetry package; Helm: generic-service exists, Phase 2 adds second values file |
| `architecture-overview.md` | Services section: describe CI caller + base Helm chart model |
| `platform-design.md` | Repo structure: generic-service + values/<service>/; add Decisions row on service independence vs shared infra |

Sections not touched: Phase 3 GitOps, Phase 4 incident assistant, Why This Design.

---

## What "Adding a Service" Looks Like After This

1. Create `services/<new-service>/` with Dockerfile and application code
2. Copy `.github/workflows/_service-template.yml` → `.github/workflows/<new-service>.yml`, set service name
3. Add `helm/values/<new-service>/values.yaml`

Result: CI runs on push, Helm deploys via Argo CD (Phase 3), Grafana shows logs/traces/metrics.

---

## Naming Rationale

`generic-service` is the community-standard name for this pattern (referenced in Helm community writing and platform engineering resources). It communicates "parameterized chart for any standard service" without being tied to a specific service or platform name.

---

## Out of Scope

- `build-push` composite action implementation (Phase 1 completion work, separate plan)
- Second service (Phase 2)
- Argo CD ApplicationSet setup (Phase 3)
- Grafana dashboard template (Phase 2)

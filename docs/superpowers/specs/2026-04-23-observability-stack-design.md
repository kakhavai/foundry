# Observability Stack Design

**Date:** 2026-04-23
**Branch:** ci/remove-orphaned-reusable-wf → will be implemented on a feature branch
**Scope:** `infra/grafana-stack/` — the receiving and visualization side only. Service instrumentation (OTel SDK in github-stats) is a separate concern.

---

## Goal

Deploy a local observability stack into the `monitoring` namespace of the Kind cluster (`foundry`). The stack receives OTLP signals from the github-stats service and makes them visible in Grafana. Deployed and managed via Helmfile — no hand-rolled Kubernetes manifests.

---

## File Layout

```
infra/grafana-stack/
├── helmfile.yaml
├── values/
│   ├── otel-collector.yaml
│   ├── loki.yaml
│   ├── tempo.yaml
│   ├── prometheus.yaml
│   └── grafana.yaml
└── dashboards/
    └── github-stats.json
```

---

## Components

All 5 releases live in the `monitoring` namespace. Each component is deployed from a maintained community Helm chart; the values files contain only overrides from chart defaults.

| Release | Chart | Mode |
|---|---|---|
| `otel-collector` | `open-telemetry/opentelemetry-collector` | `deployment` (single instance) |
| `loki` | `grafana/loki` | `singleBinary` |
| `tempo` | `grafana/tempo` | single binary |
| `prometheus` | `prometheus-community/prometheus` | server only |
| `grafana` | `grafana/grafana` | default |

---

## Helmfile

`helmfile.yaml` declares:
- 4 chart repositories (open-telemetry, grafana, prometheus-community)
- 5 releases, each pointing at its values override file
- `monitoring` namespace on all releases

---

## Component Configuration

### OTel Collector
- Mode: `deployment`
- Receivers: `otlp` (gRPC :4317, HTTP :4318)
- Exporters:
  - `otlphttp` → Loki `:3100` (Loki native OTLP, supported since v2.9)
  - `otlp` → Tempo `:4317`
  - `prometheus` exporter on `:8889` (Prometheus scrapes this)
- No custom processors for Phase 1

### Loki
- `singleBinary` mode
- `auth_enabled: false` (single-tenant local)
- Filesystem storage (ephemeral — no PVC required for local Kind)

### Tempo
- Single binary mode
- Filesystem storage (ephemeral)
- OTLP receiver enabled on `:4317`

### Prometheus
- Server only — alertmanager, pushgateway, and node-exporter disabled
- Scrape targets:
  - OTel Collector: `otel-collector.monitoring.svc:8889/metrics`
  - github-stats: `github-stats.default.svc:8000/metrics`

### Grafana
- Datasources provisioned via values (no manual UI configuration):
  - Prometheus: `http://prometheus-server.monitoring.svc.cluster.local`
  - Loki: `http://loki.monitoring.svc.cluster.local:3100`
  - Tempo: `http://tempo.monitoring.svc.cluster.local:3100`
- Dashboard sidecar provisioner enabled, pointing at `dashboards/` directory mounted as a ConfigMap

---

## Data Flow

```
github-stats (default namespace, :8000)
    │
    │ OTLP gRPC (:4317)
    ▼
otel-collector.monitoring.svc
    ├── otlphttp → loki.monitoring.svc:3100         (logs)
    ├── otlp    → tempo.monitoring.svc:4317         (traces)
    └── prometheus exporter :8889                   (metrics)

prometheus-server.monitoring.svc
    ├── scrapes otel-collector.monitoring.svc:8889/metrics
    └── scrapes github-stats.default.svc:8000/metrics

grafana.monitoring.svc:3000
    ├── Prometheus datasource
    ├── Loki datasource
    └── Tempo datasource
```

---

## Starter Dashboard

`infra/grafana-stack/dashboards/github-stats.json` — loaded automatically by Grafana's sidecar provisioner.

Panels:
- Request rate (req/sec by endpoint)
- Error rate (4xx, 5xx)
- P50 / P95 / P99 latency
- Log stream panel
- Trace search link (links to Tempo datasource)

---

## Existing Code Change

`helm/charts/github-stats/values.yaml`:
```yaml
# before
otel:
  endpoint: "http://otel-collector:4317"

# after
otel:
  endpoint: "http://otel-collector.monitoring.svc.cluster.local:4317"
```

This is required because github-stats runs in `default` and the collector runs in `monitoring` — cross-namespace DNS requires the full FQDN.

---

## What Is Not In Scope

- OTel SDK instrumentation in the github-stats Python service
- Persistent storage / PVCs for any component
- Alertmanager or alerting rules
- Multi-replica / HA configuration
- GitOps (ArgoCD/Flux) — local `helmfile apply` only

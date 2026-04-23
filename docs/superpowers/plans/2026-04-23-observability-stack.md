# Observability Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a 5-component observability stack (OTel Collector, Loki, Tempo, Prometheus, Grafana) into the `monitoring` namespace of the local Kind cluster via Helmfile.

**Architecture:** Five community Helm charts managed by a single `helmfile.yaml`. Each chart gets a slim values override file. The OTel Collector is the central hub — it receives OTLP signals from github-stats and fans out to Loki (logs), Tempo (traces), and a Prometheus scrape endpoint (metrics). Grafana reads from all three backends with datasources provisioned as code.

**Tech Stack:** Helmfile, Helm, Kind (cluster: `foundry`), open-telemetry/opentelemetry-collector chart, grafana/loki chart, grafana/tempo chart, prometheus-community/prometheus chart, grafana/grafana chart.

---

## File Map

| Action | Path |
|---|---|
| Remove | `infra/grafana-stack/.gitkeep` |
| Create | `infra/grafana-stack/helmfile.yaml` |
| Create | `infra/grafana-stack/values/otel-collector.yaml` |
| Create | `infra/grafana-stack/values/loki.yaml` |
| Create | `infra/grafana-stack/values/tempo.yaml` |
| Create | `infra/grafana-stack/values/prometheus.yaml` |
| Create | `infra/grafana-stack/values/grafana.yaml` |
| Create | `infra/grafana-stack/dashboards/github-stats.json` |
| Modify | `helm/charts/github-stats/values.yaml` — update `otel.endpoint` to FQDN |

---

## Task 1: Helmfile Scaffold

**Files:**
- Remove: `infra/grafana-stack/.gitkeep`
- Create: `infra/grafana-stack/helmfile.yaml`

- [ ] **Step 1: Remove the gitkeep**

```bash
rm infra/grafana-stack/.gitkeep
```

- [ ] **Step 2: Write `infra/grafana-stack/helmfile.yaml`**

```yaml
repositories:
  - name: open-telemetry
    url: https://open-telemetry.github.io/opentelemetry-helm-charts
  - name: grafana
    url: https://grafana.github.io/helm-charts
  - name: prometheus-community
    url: https://prometheus-community.github.io/helm-charts

releases:
  - name: otel-collector
    namespace: monitoring
    createNamespace: true
    chart: open-telemetry/opentelemetry-collector
    values:
      - values/otel-collector.yaml

  - name: loki
    namespace: monitoring
    chart: grafana/loki
    values:
      - values/loki.yaml

  - name: tempo
    namespace: monitoring
    chart: grafana/tempo
    values:
      - values/tempo.yaml

  - name: prometheus
    namespace: monitoring
    chart: prometheus-community/prometheus
    values:
      - values/prometheus.yaml

  - name: grafana
    namespace: monitoring
    chart: grafana/grafana
    values:
      - values/grafana.yaml
```

- [ ] **Step 3: Add chart repos and lint**

```bash
cd infra/grafana-stack
helmfile repos
helmfile lint
```

Expected: `Linting release=otel-collector` × 5, no errors.

- [ ] **Step 4: Commit**

```bash
git add infra/grafana-stack/helmfile.yaml
git commit -m "feat(obs): add helmfile scaffold for observability stack"
```

---

## Task 2: OTel Collector

**Files:**
- Create: `infra/grafana-stack/values/otel-collector.yaml`

- [ ] **Step 1: Write `infra/grafana-stack/values/otel-collector.yaml`**

```yaml
mode: deployment

config:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318

  exporters:
    otlphttp/loki:
      endpoint: http://loki.monitoring.svc.cluster.local:3100/otlp
    otlp/tempo:
      endpoint: tempo.monitoring.svc.cluster.local:4317
      tls:
        insecure: true
    prometheus:
      endpoint: 0.0.0.0:8889

  service:
    pipelines:
      logs:
        receivers: [otlp]
        exporters: [otlphttp/loki]
      traces:
        receivers: [otlp]
        exporters: [otlp/tempo]
      metrics:
        receivers: [otlp]
        exporters: [prometheus]

ports:
  prometheus:
    enabled: true
    containerPort: 8889
    servicePort: 8889
    protocol: TCP
```

- [ ] **Step 2: Lint**

```bash
cd infra/grafana-stack
helmfile lint -l name=otel-collector
```

Expected: no errors.

- [ ] **Step 3: Apply and verify**

```bash
cd infra/grafana-stack
helmfile apply -l name=otel-collector
kubectl rollout status deployment/otel-collector-opentelemetry-collector -n monitoring
```

Expected: `deployment "otel-collector-opentelemetry-collector" successfully rolled out`

- [ ] **Step 4: Verify metrics port is reachable**

```bash
kubectl port-forward -n monitoring svc/otel-collector-opentelemetry-collector 8889:8889 &
curl -s http://localhost:8889/metrics | head -5
kill %1
```

Expected: Prometheus text format output starting with `# HELP` lines.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana-stack/values/otel-collector.yaml
git commit -m "feat(obs): add OTel Collector deployment"
```

---

## Task 3: Loki

**Files:**
- Create: `infra/grafana-stack/values/loki.yaml`

- [ ] **Step 1: Write `infra/grafana-stack/values/loki.yaml`**

```yaml
deploymentMode: SingleBinary

loki:
  auth_enabled: false
  commonConfig:
    replication_factor: 1
  storage:
    type: filesystem
  limits_config:
    allow_structured_metadata: true
    volume_enabled: true

singleBinary:
  replicas: 1

backend:
  replicas: 0
read:
  replicas: 0
write:
  replicas: 0

minio:
  enabled: false

gateway:
  enabled: false
```

- [ ] **Step 2: Lint**

```bash
cd infra/grafana-stack
helmfile lint -l name=loki
```

Expected: no errors.

- [ ] **Step 3: Apply and verify**

```bash
cd infra/grafana-stack
helmfile apply -l name=loki
kubectl rollout status statefulset/loki -n monitoring
```

Expected: `statefulset rolling update complete`

- [ ] **Step 4: Verify Loki ready endpoint**

```bash
kubectl port-forward -n monitoring svc/loki 3100:3100 &
curl -s http://localhost:3100/ready
kill %1
```

Expected: `ready`

- [ ] **Step 5: Commit**

```bash
git add infra/grafana-stack/values/loki.yaml
git commit -m "feat(obs): add Loki single binary deployment"
```

---

## Task 4: Tempo

**Files:**
- Create: `infra/grafana-stack/values/tempo.yaml`

- [ ] **Step 1: Write `infra/grafana-stack/values/tempo.yaml`**

```yaml
tempo:
  reportingEnabled: false
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
  storage:
    trace:
      backend: local
      local:
        path: /var/tempo/traces
      wal:
        path: /var/tempo/wal
```

- [ ] **Step 2: Lint**

```bash
cd infra/grafana-stack
helmfile lint -l name=tempo
```

Expected: no errors.

- [ ] **Step 3: Apply and verify**

```bash
cd infra/grafana-stack
helmfile apply -l name=tempo
kubectl rollout status deployment/tempo -n monitoring
```

Expected: `deployment "tempo" successfully rolled out`

- [ ] **Step 4: Verify Tempo ready endpoint**

```bash
kubectl port-forward -n monitoring svc/tempo 3100:3100 &
curl -s http://localhost:3100/ready
kill %1
```

Expected: `ready`

- [ ] **Step 5: Commit**

```bash
git add infra/grafana-stack/values/tempo.yaml
git commit -m "feat(obs): add Tempo single binary deployment"
```

---

## Task 5: Prometheus

**Files:**
- Create: `infra/grafana-stack/values/prometheus.yaml`

- [ ] **Step 1: Write `infra/grafana-stack/values/prometheus.yaml`**

```yaml
alertmanager:
  enabled: false

prometheus-pushgateway:
  enabled: false

kube-state-metrics:
  enabled: false

prometheus-node-exporter:
  enabled: false

server:
  retention: 24h

extraScrapeConfigs: |
  - job_name: otel-collector
    static_configs:
      - targets:
          - otel-collector.monitoring.svc.cluster.local:8889
  - job_name: github-stats
    static_configs:
      - targets:
          - github-stats.default.svc.cluster.local:8000
```

- [ ] **Step 2: Lint**

```bash
cd infra/grafana-stack
helmfile lint -l name=prometheus
```

Expected: no errors.

- [ ] **Step 3: Apply and verify**

```bash
cd infra/grafana-stack
helmfile apply -l name=prometheus
kubectl rollout status deployment/prometheus-server -n monitoring
```

Expected: `deployment "prometheus-server" successfully rolled out`

- [ ] **Step 4: Verify scrape targets are configured**

```bash
kubectl port-forward -n monitoring svc/prometheus-server 9090:80 &
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool | grep '"job"'
kill %1
```

Expected: output includes `"otel-collector"` and `"github-stats"` job names.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana-stack/values/prometheus.yaml
git commit -m "feat(obs): add Prometheus server with scrape configs"
```

---

## Task 6: Grafana Datasources

**Files:**
- Create: `infra/grafana-stack/values/grafana.yaml`

- [ ] **Step 1: Write `infra/grafana-stack/values/grafana.yaml`**

```yaml
adminPassword: admin

datasources:
  datasources.yaml:
    apiVersion: 1
    datasources:
      - name: Prometheus
        type: prometheus
        uid: prometheus
        url: http://prometheus-server.monitoring.svc.cluster.local
        isDefault: true
        access: proxy
      - name: Loki
        type: loki
        uid: loki
        url: http://loki.monitoring.svc.cluster.local:3100
        access: proxy
      - name: Tempo
        type: tempo
        uid: tempo
        url: http://tempo.monitoring.svc.cluster.local:3100
        access: proxy
        jsonData:
          tracesToLogsV2:
            datasourceUid: loki
          lokiSearch:
            datasourceUid: loki
```

- [ ] **Step 2: Lint**

```bash
cd infra/grafana-stack
helmfile lint -l name=grafana
```

Expected: no errors.

- [ ] **Step 3: Apply and verify**

```bash
cd infra/grafana-stack
helmfile apply -l name=grafana
kubectl rollout status deployment/grafana -n monitoring
```

Expected: `deployment "grafana" successfully rolled out`

- [ ] **Step 4: Verify all 3 datasources are healthy**

```bash
kubectl port-forward -n monitoring svc/grafana 3000:80 &
curl -s -u admin:admin http://localhost:3000/api/datasources | python3 -m json.tool | grep '"name"'
kill %1
```

Expected: output includes `"Prometheus"`, `"Loki"`, `"Tempo"`.

- [ ] **Step 5: Commit**

```bash
git add infra/grafana-stack/values/grafana.yaml
git commit -m "feat(obs): add Grafana with provisioned datasources"
```

---

## Task 7: Starter Dashboard

**Files:**
- Create: `infra/grafana-stack/dashboards/github-stats.json`
- Modify: `infra/grafana-stack/values/grafana.yaml` — add `dashboards` section

- [ ] **Step 1: Write `infra/grafana-stack/dashboards/github-stats.json`**

Metric names follow OTel HTTP semantic conventions as emitted by `opentelemetry-instrumentation-fastapi`. After the OTel Collector prometheus exporter, dots become underscores: `http.server.request.duration` → `http_server_request_duration_seconds`.

```json
{
  "annotations": { "list": [] },
  "editable": true,
  "graphTooltip": 0,
  "id": null,
  "links": [],
  "panels": [
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "reqps" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "id": 1,
      "options": {
        "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "single" }
      },
      "targets": [
        {
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum(rate(http_server_request_duration_seconds_count{job=\"github-stats\"}[5m])) by (http_route)",
          "legendFormat": "{{ http_route }}"
        }
      ],
      "title": "Request Rate",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "percentunit" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "id": 2,
      "options": {
        "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "single" }
      },
      "targets": [
        {
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum(rate(http_server_request_duration_seconds_count{job=\"github-stats\",http_response_status_code=~\"4..|5..\"}[5m])) / sum(rate(http_server_request_duration_seconds_count{job=\"github-stats\"}[5m]))",
          "legendFormat": "error rate"
        }
      ],
      "title": "Error Rate",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "fieldConfig": { "defaults": { "unit": "s" }, "overrides": [] },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 8 },
      "id": 3,
      "options": {
        "legend": { "calcs": [], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "multi" }
      },
      "targets": [
        {
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "histogram_quantile(0.50, sum(rate(http_server_request_duration_seconds_bucket{job=\"github-stats\"}[5m])) by (le))",
          "legendFormat": "P50"
        },
        {
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "histogram_quantile(0.95, sum(rate(http_server_request_duration_seconds_bucket{job=\"github-stats\"}[5m])) by (le))",
          "legendFormat": "P95"
        },
        {
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "histogram_quantile(0.99, sum(rate(http_server_request_duration_seconds_bucket{job=\"github-stats\"}[5m])) by (le))",
          "legendFormat": "P99"
        }
      ],
      "title": "Latency P50 / P95 / P99",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "loki", "uid": "loki" },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 16 },
      "id": 4,
      "options": {
        "dedupStrategy": "none",
        "showLabels": false,
        "showTime": true,
        "sortOrder": "Descending",
        "wrapLogMessage": false
      },
      "targets": [
        {
          "datasource": { "type": "loki", "uid": "loki" },
          "expr": "{service_name=\"github-stats\"}",
          "legendFormat": ""
        }
      ],
      "title": "Log Stream",
      "type": "logs"
    },
    {
      "datasource": { "type": "tempo", "uid": "tempo" },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 24 },
      "id": 5,
      "options": {},
      "targets": [
        {
          "datasource": { "type": "tempo", "uid": "tempo" },
          "filters": [
            {
              "id": "service-name",
              "operator": "=",
              "scope": "resource",
              "tag": "service.name",
              "value": "github-stats",
              "valueType": "string"
            }
          ],
          "queryType": "traceqlSearch"
        }
      ],
      "title": "Trace Search",
      "type": "traces"
    }
  ],
  "refresh": "30s",
  "schemaVersion": 38,
  "tags": ["github-stats", "observability"],
  "templating": { "list": [] },
  "time": { "from": "now-1h", "to": "now" },
  "timepicker": {},
  "timezone": "browser",
  "title": "github-stats",
  "uid": "github-stats-overview",
  "version": 1
}
```

- [ ] **Step 2: Add dashboard provisioning to `infra/grafana-stack/values/grafana.yaml`**

Append this to the bottom of the existing file:

```yaml
dashboardProviders:
  dashboardproviders.yaml:
    apiVersion: 1
    providers:
      - name: default
        orgId: 1
        folder: ''
        type: file
        disableDeletion: false
        editable: true
        options:
          path: /var/lib/grafana/dashboards/default
```

The dashboard JSON is injected via helmfile's `set[].file` — no `dashboards` key needed in values.yaml. Update the `grafana` release block in `helmfile.yaml`:

```yaml
  - name: grafana
    namespace: monitoring
    chart: grafana/grafana
    values:
      - values/grafana.yaml
    set:
      - name: dashboards.default.github-stats.json
        file: dashboards/github-stats.json
```

- [ ] **Step 3: Apply and verify dashboard loads**

```bash
cd infra/grafana-stack
helmfile apply -l name=grafana
kubectl rollout status deployment/grafana -n monitoring
```

```bash
kubectl port-forward -n monitoring svc/grafana 3000:80 &
curl -s -u admin:admin http://localhost:3000/api/search | python3 -m json.tool | grep '"title"'
kill %1
```

Expected: output includes `"github-stats"`.

- [ ] **Step 4: Commit**

```bash
git add infra/grafana-stack/dashboards/github-stats.json infra/grafana-stack/values/grafana.yaml infra/grafana-stack/helmfile.yaml
git commit -m "feat(obs): add starter Grafana dashboard for github-stats"
```

---

## Task 8: Update github-stats OTel Endpoint

**Files:**
- Modify: `helm/charts/github-stats/values.yaml:21`

The OTel Collector now lives in `monitoring` namespace. Short DNS names only resolve within the same namespace, so github-stats (in `default`) needs the fully qualified domain name.

- [ ] **Step 1: Update `helm/charts/github-stats/values.yaml`**

Change:
```yaml
otel:
  endpoint: "http://otel-collector:4317"
```

To:
```yaml
otel:
  endpoint: "http://otel-collector.monitoring.svc.cluster.local:4317"
```

- [ ] **Step 2: Lint the Helm chart**

```bash
helm lint helm/charts/github-stats
```

Expected: `1 chart(s) linted, 0 chart(s) failed`

- [ ] **Step 3: Commit**

```bash
git add helm/charts/github-stats/values.yaml
git commit -m "fix(github-stats): update OTel endpoint to cross-namespace FQDN"
```

---

## Task 9: Full Stack Smoke Test

- [ ] **Step 1: Apply the full stack**

```bash
cd infra/grafana-stack
helmfile apply
```

Expected: all 5 releases show `STATUS: deployed`.

- [ ] **Step 2: Verify all pods are running**

```bash
kubectl get pods -n monitoring
```

Expected: pods for `otel-collector`, `loki`, `tempo`, `prometheus-server`, and `grafana` all showing `Running` and `1/1` or `2/2` ready.

- [ ] **Step 3: Verify Grafana datasources are healthy in the UI**

```bash
kubectl port-forward -n monitoring svc/grafana 3000:80
```

Open `http://localhost:3000` in a browser. Login: `admin` / `admin`. Navigate to **Connections → Data sources** and confirm all three datasources (Prometheus, Loki, Tempo) show a green checkmark when you click **Test**.

- [ ] **Step 4: Verify the dashboard is present**

In Grafana, navigate to **Dashboards**. Confirm `github-stats` dashboard appears. Panels will show "No data" until the service is instrumented — that is expected.

- [ ] **Step 5: Final commit and push branch**

```bash
git log --oneline -8
git push -u origin HEAD
```

Open a PR from the current branch targeting `main`.

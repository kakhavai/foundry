# Generic-Service Helm Chart + CI Caller Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the Helm chart and CI workflows to a reusable base chart + thin caller model so adding a new service requires only two files.

**Architecture:** Rename `helm/charts/github-stats/` to `helm/charts/generic-service/` — a fully parameterized chart with OTel env vars and Prometheus annotations baked in. Per-service config lives in `helm/values/<service>/values.yaml`. CI uses a GitHub Actions reusable workflow (`_service-template.yml`) that each per-service caller file invokes with just a service name.

**Tech Stack:** Helm 3, GitHub Actions (composite actions + reusable workflows), dorny/paths-filter@v3, azure/setup-helm@v5

---

## File Map

**Create:**
- `helm/charts/generic-service/Chart.yaml`
- `helm/charts/generic-service/values.yaml`
- `helm/charts/generic-service/templates/_helpers.tpl`
- `helm/charts/generic-service/templates/deployment.yaml`
- `helm/charts/generic-service/templates/service.yaml`
- `helm/charts/generic-service/templates/configmap.yaml`
- `helm/values/github-stats/values.yaml`
- `.github/workflows/_service-template.yml`
- `.github/workflows/github-stats.yml`

**Modify:**
- `.github/actions/helm-lint/action.yml` — add optional `values-file` input
- `docs/architecture/phase-1-first-paved-road.md`
- `docs/architecture/phase-2-golden-path.md`
- `docs/architecture/architecture-overview.md`
- `docs/plans/2026-04-12-platform-design.md`

**Delete:**
- `helm/charts/github-stats/` (entire directory)
- `.github/workflows/ci.yml`

---

## Task 1: Create `helm/charts/generic-service/` base chart

**Files:**
- Create: `helm/charts/generic-service/Chart.yaml`
- Create: `helm/charts/generic-service/values.yaml`
- Create: `helm/charts/generic-service/templates/_helpers.tpl`
- Create: `helm/charts/generic-service/templates/deployment.yaml`
- Create: `helm/charts/generic-service/templates/service.yaml`
- Create: `helm/charts/generic-service/templates/configmap.yaml`

- [ ] **Step 1: Create `helm/charts/generic-service/Chart.yaml`**

```yaml
apiVersion: v2
name: generic-service
description: Parameterized base chart for standard Foundry HTTP services
type: application
version: 0.1.0
appVersion: "0.1.0"
```

- [ ] **Step 2: Create `helm/charts/generic-service/values.yaml`**

These are defaults and documentation — per-service values files override them.

```yaml
replicaCount: 1

service:
  name: ""
  type: ClusterIP
  port: 8080

image:
  repository: ""
  pullPolicy: IfNotPresent
  tag: ""

containerPort: 8080

resources:
  limits:
    cpu: 250m
    memory: 256Mi
  requests:
    cpu: 100m
    memory: 128Mi

otel:
  endpoint: "http://otel-collector.monitoring.svc.cluster.local:4317"
  resourceAttributes: ""
```

- [ ] **Step 3: Create `helm/charts/generic-service/templates/_helpers.tpl`**

```
{{- define "generic-service.fullname" -}}
{{- .Release.Name }}
{{- end }}

{{- define "generic-service.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{ include "generic-service.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "generic-service.selectorLabels" -}}
app.kubernetes.io/name: {{ .Values.service.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
```

- [ ] **Step 4: Create `helm/charts/generic-service/templates/deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "generic-service.fullname" . }}
  labels:
    {{- include "generic-service.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "generic-service.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "generic-service.selectorLabels" . | nindent 8 }}
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ .Values.containerPort | quote }}
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 999
      containers:
        - name: {{ .Values.service.name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: {{ .Values.containerPort }}
              protocol: TCP
          envFrom:
            - configMapRef:
                name: {{ include "generic-service.fullname" . }}-config
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          livenessProbe:
            httpGet:
              path: /health
              port: {{ .Values.containerPort }}
            initialDelaySeconds: 5
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /health
              port: {{ .Values.containerPort }}
            initialDelaySeconds: 5
            periodSeconds: 10
```

- [ ] **Step 5: Create `helm/charts/generic-service/templates/service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "generic-service.fullname" . }}
  labels:
    {{- include "generic-service.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: {{ .Values.containerPort }}
      protocol: TCP
  selector:
    {{- include "generic-service.selectorLabels" . | nindent 4 }}
```

- [ ] **Step 6: Create `helm/charts/generic-service/templates/configmap.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "generic-service.fullname" . }}-config
  labels:
    {{- include "generic-service.labels" . | nindent 4 }}
data:
  OTEL_EXPORTER_OTLP_ENDPOINT: {{ .Values.otel.endpoint | quote }}
  OTEL_SERVICE_NAME: {{ .Values.service.name | quote }}
  OTEL_RESOURCE_ATTRIBUTES: {{ .Values.otel.resourceAttributes | quote }}
```

- [ ] **Step 7: Run `helm lint` against the base chart with default values**

```bash
helm lint helm/charts/generic-service
```

Expected: `1 chart(s) linted, 0 chart(s) failed`

If it fails, fix the template error before continuing.

- [ ] **Step 8: Commit**

```bash
git add helm/charts/generic-service/
git commit -m "feat: add generic-service base helm chart"
```

---

## Task 2: Create `helm/values/github-stats/values.yaml`

**Files:**
- Create: `helm/values/github-stats/values.yaml`

- [ ] **Step 1: Create `helm/values/github-stats/values.yaml`**

```yaml
service:
  name: github-stats
  port: 8000

image:
  repository: ghcr.io/kakhavai/foundry/github-stats

containerPort: 8000

resources:
  limits:
    cpu: 250m
    memory: 256Mi
  requests:
    cpu: 100m
    memory: 128Mi
```

- [ ] **Step 2: Run `helm lint` with the github-stats values file**

```bash
helm lint helm/charts/generic-service -f helm/values/github-stats/values.yaml
```

Expected: `1 chart(s) linted, 0 chart(s) failed`

- [ ] **Step 3: Render and verify the Deployment includes OTel env vars and Prometheus annotations**

```bash
helm template github-stats helm/charts/generic-service -f helm/values/github-stats/values.yaml
```

In the output, verify:
- The `Deployment` has `prometheus.io/scrape: "true"` and `prometheus.io/port: "8000"` under `template.metadata.annotations`
- The `ConfigMap` has `OTEL_SERVICE_NAME: github-stats` and `OTEL_EXPORTER_OTLP_ENDPOINT` set
- Resource names are `github-stats` (from the release name in the template command)
- `app.kubernetes.io/name: github-stats` in labels

- [ ] **Step 4: Commit**

```bash
git add helm/values/github-stats/values.yaml
git commit -m "feat: add github-stats per-service helm values"
```

---

## Task 3: Update `helm-lint` action to support `values-file`

**Files:**
- Modify: `.github/actions/helm-lint/action.yml`

- [ ] **Step 1: Replace `.github/actions/helm-lint/action.yml` with the updated version**

```yaml
name: Helm lint
description: Runs helm lint on a chart

inputs:
  chart-path:
    required: true
    description: Path to the Helm chart relative to repo root (e.g. helm/charts/generic-service)
  values-file:
    required: false
    description: Path to a values file for lint (e.g. helm/values/github-stats/values.yaml)

runs:
  using: composite
  steps:
    - uses: azure/setup-helm@v5
      with:
        helm-version: "v3.17.3"
    - name: Lint chart
      shell: bash
      run: |
        if [ -n "${{ inputs.values-file }}" ]; then
          helm lint ${{ inputs.chart-path }} -f ${{ inputs.values-file }}
        else
          helm lint ${{ inputs.chart-path }}
        fi
```

- [ ] **Step 2: Verify the YAML is valid**

```bash
python -c "import yaml; yaml.safe_load(open('.github/actions/helm-lint/action.yml'))" && echo "valid"
```

Expected: `valid`

- [ ] **Step 3: Commit**

```bash
git add .github/actions/helm-lint/action.yml
git commit -m "feat: add optional values-file input to helm-lint action"
```

---

## Task 4: Create CI reusable workflow and github-stats caller; delete old files

**Files:**
- Create: `.github/workflows/_service-template.yml`
- Create: `.github/workflows/github-stats.yml`
- Delete: `.github/workflows/ci.yml`
- Delete: `helm/charts/github-stats/` (entire directory)

- [ ] **Step 1: Create `.github/workflows/_service-template.yml`**

This is a GitHub Actions reusable workflow. Per-service callers invoke it with `uses:` and pass the service name as an input. It handles path filtering, lint/test, and Helm lint for any service.

```yaml
name: Service CI Template

on:
  workflow_call:
    inputs:
      service-name:
        required: true
        type: string

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      changed: ${{ steps.filter.outputs.changed }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            changed:
              - 'services/${{ inputs.service-name }}/**'
              - 'helm/values/${{ inputs.service-name }}/**'
              - 'helm/charts/generic-service/**'

  lint-test:
    needs: changes
    if: needs.changes.outputs.changed == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-lint-test
        with:
          working-directory: services/${{ inputs.service-name }}

  helm-lint:
    needs: changes
    if: needs.changes.outputs.changed == 'true'
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/helm-lint
        with:
          chart-path: helm/charts/generic-service
          values-file: helm/values/${{ inputs.service-name }}/values.yaml
```

- [ ] **Step 2: Create `.github/workflows/github-stats.yml`**

```yaml
name: github-stats

on:
  pull_request:
  push:
    branches:
      - main

jobs:
  ci:
    uses: ./.github/workflows/_service-template.yml
    with:
      service-name: github-stats
```

- [ ] **Step 3: Delete `helm/charts/github-stats/`**

```bash
git rm -r helm/charts/github-stats/
```

Expected: removes `Chart.yaml`, `values.yaml`, and all files under `templates/`.

- [ ] **Step 4: Delete `.github/workflows/ci.yml`**

```bash
git rm .github/workflows/ci.yml
```

- [ ] **Step 5: Validate YAML for both new workflow files**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/_service-template.yml')); yaml.safe_load(open('.github/workflows/github-stats.yml')); print('valid')"
```

Expected: `valid`

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/_service-template.yml .github/workflows/github-stats.yml
git commit -m "feat: add reusable CI template and github-stats caller; remove ci.yml and per-service helm chart"
```

---

## Task 5: Update `phase-1-first-paved-road.md`

**Files:**
- Modify: `docs/architecture/phase-1-first-paved-road.md`

Two sections need updating: CI and Helm Chart. Everything else stays.

- [ ] **Step 1: Replace the GitHub Actions CI section**

Find:
```
### GitHub Actions CI
Three jobs on push to `services/github-stats/**` or `helm/charts/github-stats/**`:
1. `lint-test` — runs `ruff` (lint) and `pytest`
2. `build-push` — builds and pushes image to GHCR, tagged with Git SHA
3. `helm-lint` — runs `helm lint` on the chart
```

Replace with:
```
### GitHub Actions CI
Thin caller workflow at `.github/workflows/github-stats.yml` triggers on changes to `services/github-stats/**`, `helm/values/github-stats/**`, or `helm/charts/generic-service/**`. Delegates to `.github/workflows/_service-template.yml` (reusable workflow) which runs:
1. `lint-test` — runs `ruff` (lint) and `pytest` via the `python-lint-test` composite action
2. `build-push` — builds and pushes image to GHCR, tagged with Git SHA
3. `helm-lint` — runs `helm lint` on `helm/charts/generic-service` with `helm/values/github-stats/values.yaml`
```

- [ ] **Step 2: Replace the Helm Chart section**

Find:
```
### Helm Chart
A standard Helm chart under `helm/charts/github-stats/` with:
- `Deployment` with configurable replicas, image tag, resource limits
- `Service` (ClusterIP)
- `ConfigMap` for OTel endpoint and env config
- `values.yaml` with sensible defaults
```

Replace with:
```
### Helm Chart
Base chart at `helm/charts/generic-service/` — one parameterized chart used by every standard HTTP service:
- `Deployment` with configurable replicas, image tag, resource limits, and containerPort
- `Service` (ClusterIP)
- `ConfigMap` with OTel env vars injected automatically (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`)
- Pod annotations for Prometheus auto-discovery (`prometheus.io/scrape`, `prometheus.io/port`)

Per-service config at `helm/values/github-stats/values.yaml` — contains only service-specific values (image, port, resources). No observability config needed per service.
```

- [ ] **Step 3: Update the Deliverables section**

Find:
```
- `helm/charts/github-stats/` — working Helm chart
```

Replace with:
```
- `helm/charts/generic-service/` — parameterized base Helm chart
- `helm/values/github-stats/values.yaml` — github-stats service values
```

Find:
```
- `.github/workflows/github-stats.yml` — CI pipeline
```

Replace with:
```
- `.github/workflows/_service-template.yml` — reusable CI template
- `.github/workflows/github-stats.yml` — github-stats CI caller
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture/phase-1-first-paved-road.md
git commit -m "docs: update phase-1 to reflect generic-service chart and CI caller model"
```

---

## Task 6: Update `phase-2-golden-path.md`

**Files:**
- Modify: `docs/architecture/phase-2-golden-path.md`

Remove the `shared-*.yml` CI section and `foundry_telemetry` section. Replace with accurate descriptions. Update deliverables.

- [ ] **Step 1: Replace the "Reusable CI Workflow Templates" section**

Find:
```
### Reusable CI Workflow Templates
Common CI steps extracted into reusable GitHub Actions workflows under `.github/workflows/shared-*.yml`:
- `shared-lint-test.yml` — parameterized lint and test job
- `shared-build-push.yml` — parameterized build and push job
- `shared-helm-lint.yml` — Helm validation job

Each service's workflow file becomes a thin caller that passes service-specific parameters.
```

Replace with:
```
### CI Caller Pattern
The reusable CI template (`.github/workflows/_service-template.yml`) and composite actions were established in Phase 1. Onboarding the second service requires one new file: `.github/workflows/<second-service>.yml`, a thin caller that invokes the template with the service name. No CI logic is duplicated.
```

- [ ] **Step 2: Replace the "Standard Telemetry Setup" section**

Find:
```
### Standard Telemetry Setup
A shared Python package or copy-paste module (`foundry_telemetry`) that any service imports to get:
- OTel SDK initialization (traces + metrics + logs)
- Standard resource attributes (service name, version, environment)
- Prometheus metrics endpoint wiring
```

Replace with:
```
### Observability
OTel configuration is provided by the `generic-service` base chart via env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`) and Prometheus pod annotations — established in Phase 1. Each service instruments itself with the OTel Python SDK. No shared library required; no per-service observability config required.
```

- [ ] **Step 3: Update the Deliverables section**

Find:
```
- `.github/workflows/shared-*.yml` — reusable CI templates
```

Replace with:
```
- `.github/workflows/<second-service>.yml` — second service CI caller
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture/phase-2-golden-path.md
git commit -m "docs: update phase-2 to remove shared-*.yml and foundry_telemetry; reflect actual CI and OTel model"
```

---

## Task 7: Update `architecture-overview.md`

**Files:**
- Modify: `docs/architecture/architecture-overview.md`

- [ ] **Step 1: Replace the Services section**

Find:
```
### Services
Services live in `services/<name>/`. Each service:
- has its own Dockerfile
- emits structured logs, traces, and metrics via the OpenTelemetry SDK
- exports telemetry to the OTel Collector via OTLP

The first service is `github-stats` — a Python HTTP API that pulls and exposes GitHub activity data.
```

Replace with:
```
### Services
Services live in `services/<name>/`. Each service owns its own Dockerfile, dependency lockfile, and application code — no shared Python libraries across services. What is shared is infrastructure.

**CI:** A thin caller workflow (`.github/workflows/<service-name>.yml`) calls `.github/workflows/_service-template.yml`, delegating to composite actions for lint/test and Helm lint. Adding a service = add one caller file (~10 lines).

**Deployment:** `helm/charts/generic-service/` is a single parameterized base chart used by every standard HTTP service. Adding a service = add `helm/values/<service-name>/values.yaml`. The base chart automatically injects OTel env vars and Prometheus pod annotations — every service gets full observability with zero per-service observability config.

The first service is `github-stats` — a Python HTTP API that pulls and exposes GitHub activity data.
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/architecture-overview.md
git commit -m "docs: update architecture overview services section to reflect CI caller and generic-service model"
```

---

## Task 8: Update `platform-design.md`

**Files:**
- Modify: `docs/plans/2026-04-12-platform-design.md`

- [ ] **Step 1: Replace the Repo Structure section**

Find:
```
  helm/
    charts/              # Helm charts per service
```

Replace with:
```
  helm/
    charts/
      generic-service/   # Parameterized base chart for all standard HTTP services
    values/
      github-stats/      # Per-service Helm values
```

- [ ] **Step 2: Add two rows to the Decisions table**

Find the last row of the Decisions table (the AI layer row):
```
| AI layer | Assistive triage CLI (Claude API) | Reduces MTTR; no autonomous action; correct scope for system maturity |
```

Replace with:
```
| AI layer | Assistive triage CLI (Claude API) | Reduces MTTR; no autonomous action; correct scope for system maturity |
| Service independence | Each service owns its own deps, lockfile, and Dockerfile — no shared Python libs | Services are heterogeneous; shared libs create coupling without proportional benefit |
| Shared platform infra | One base Helm chart (`generic-service`), one CI template (`_service-template.yml`), one observability stack | Avoids N-way duplication; adding a service requires two files, not a new chart directory |
```

- [ ] **Step 3: Commit**

```bash
git add docs/plans/2026-04-12-platform-design.md
git commit -m "docs: update platform design repo structure and decisions to reflect generic-service model"
```

---

## Self-Review Notes

- Task 1 creates the chart; Task 2 creates the values file; `helm lint` is run after each — no placeholder verification steps.
- `containerPort` is consistently named and typed as an integer in `values.yaml` and referenced as `{{ .Values.containerPort }}` throughout templates.
- The `helm-lint` action update (Task 3) happens before the CI workflow files reference it (Task 4) — correct ordering.
- The `_service-template.yml` in Task 4 references `.github/actions/helm-lint` and `.github/actions/python-lint-test` which both exist.
- Path filter in `_service-template.yml` covers `helm/charts/generic-service/**` so a chart template change triggers CI for the service — addresses the gap noted in the spec self-review.
- Doc tasks (5-8) are independent of each other and can be done in any order.

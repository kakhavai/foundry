# Foundry — Claude Code Context

## What This Repo Is

Foundry is a **production-grade Kubernetes platform monorepo**. The primary purpose is to demonstrate how a platform team would deliver a scalable, observable, GitOps-driven service delivery system — covering CI/CD, Helm-based deployment, ArgoCD GitOps, and integrated observability via OpenTelemetry and the Grafana LGTM stack.

The application running on this platform is a **fantasy football prediction product** — a frontend, internal backend, and a managed fleet of data and projection services. The platform exists independently of the app; the app proves the platform under real-world conditions.

The platform is built incrementally. Each phase proves a pattern, then the next phase adds a new service that reuses it.

---

## Long-Term Vision: Fantasy Football Platform

The end-state is a fully managed fantasy football product running on Foundry:

```
collectors — IN this repo
  └── weather (the first), betting lines, social/news feeds, injury + health
      reports, field type, and others; each gathers one kind of raw signal
  └── each exposes an HTTP API behind ONE gateway, routed by path:
        https://<gateway-host>/collectors/weather/...
        https://<gateway-host>/collectors/betting-lines/...

        ↑  IN: the generator CALLS the collector APIs, authenticating with a
           bearer token per collector. Adding a collector = one ingress path
           rule + one Secret, not a new hostname or certificate.

projections generator — NOT in this repo, runs privately
  └── calls the collector APIs, combines them with proprietary sources
  └── the ML / ranking methodology is the product's value and stays out of
      version control entirely
  └── writes one projections snapshot per scoring format to S3, on a cadence
      or dispatched by hand

        ↓  OUT: S3 is how results come BACK to the platform.
           Contract: contracts/projections-snapshot/

player-projections — IN this repo
  └── polls the snapshots, serves them at GET /projections

fantasy-frontend (future) — IN this repo
  └── user-facing web UI consuming the projections API
```

Two integration surfaces, not one, and they run in opposite directions. The
generator reaches **in** over authenticated HTTP; results come **back** as a
file in S3. Foundry itself never calls out to the generator.

**There is no `player-data` service, and there is not going to be one.** Earlier
revisions of this document described an auth-gated internal backend by that
name. That was wrong. The producer is an **offline generator that runs outside
this repository** — Foundry does not deploy it, does not call it, and cannot
observe it. If you find yourself looking for `services/player-data/`, it does
not exist by design.

That is also why the contract lives in `contracts/projections-snapshot/` and is
named for the **artifact** rather than a producer: the artifact is the only part
of the producer Foundry ever sees.

### How the generator reaches the collectors — decided

Collectors are **services with HTTP APIs**, not jobs that drop files. Three
decisions, settled together:

| Question | Decision |
|---|---|
| Discovery | **One gateway, path-routed.** A single hostname and TLS certificate; each collector is a path under `/collectors/<name>/`. "Centralized" means one front door, not a registry service. |
| Auth | **Bearer token per collector.** Stored as a Kubernetes Secret, injected via the `extraEnv` + `secretKeyRef` pattern documented below. The generator sends `Authorization: Bearer <token>`. |
| Rotation / Phase 6 | Tokens rotate by updating the Secret. On EKS the Secret is backed by AWS Secrets Manager — the service-side code path does not change. |

Chosen over mTLS and OIDC deliberately: the generator is a single external
client on a known machine, so a rotatable shared secret is the simplest thing
that works. Revisit if the generator ever becomes multi-tenant, or if a token
leaking into a log becomes a realistic concern — mTLS removes the bearer secret
entirely, and OIDC removes the long-lived one.

`weather` is the first collector. It is served at `/collectors/weather/` through
the gateway and enforces a bearer token in-process.

**Gateway:** Envoy Gateway (pinned, `infra/gateway/`) implementing Gateway API.
One `Gateway` named `foundry` in `envoy-gateway-system` holds the front door;
each collector's Helm chart templates its own `HTTPRoute` via `gateway.enabled`,
`gateway.pathPrefix`, and `gateway.publicPaths` in its values file. On Kind it
is reachable at `http://localhost:8080` through the NodePort 30080 mapping in
`infra/kind/cluster.yaml`.

The gateway strips `/collectors/<name>` and rewrites onto each declared
`publicPaths` entry — the gateway publishes only a collector's declared
contract paths, not its whole route surface. `weather`'s routes moved off the
`/weather/` prefix during Phase 8's 8A retrofit, so the doubled segment that
used to appear in the external path (`/collectors/weather/weather/stadiums`) is
gone; the external path today reads `/collectors/weather/signals`. `/health`
and `/metrics` are exempt from bearer auth in-process (so the kubelet's probes
and Prometheus's scrape work) but are deliberately **not** in `publicPaths`, so
they 404 at the gateway and only answer in-cluster.

**Auth is enforced in the service, not at the gateway**
(`libs/collector-core/collector_core/auth.py`, mounted by every collector's
`build_collector_app` call — see `collector_core/app.py`). Middleware, so a
route added later is protected by default. `/health` and
`/metrics` are exempt because the kubelet's probes and Prometheus's annotation
scrape cannot carry a token. An absent or empty `COLLECTOR_TOKEN` returns 503
on every data route — it fails closed, so a Secret that never syncs is loud
rather than an open collector.

Gateway-only enforcement was rejected because `scripts/smoke-test.sh`
port-forwards `svc/weather` directly: it would have left the required
`integration-test` check green over an unprotected path.

**Rotation requires a rollout.** `secretKeyRef` injects the token as an env var
captured at pod start, so updating the Secret takes effect only after
`kubectl rollout restart deploy/<service>`.

**Local development:** `scripts/deploy-local.py` creates the Secret with the
literal `local-dev-token`. That value is Kind-only and committed deliberately;
real tokens are created out of band and never enter Git.

**The Secret is not managed by GitOps — nothing in `infra/gitops/` creates it.**
On a local Kind cluster, `scripts/deploy-local.py` creates it, as above. On an
ArgoCD-managed cluster (or Phase 6's EKS, where it is backed by AWS Secrets
Manager) it must be created before or alongside the first sync — `argocd-deploy.py`
does not do this. Because `optional: true` lets the pod start without it, and
`/health` doesn't check for it, the symptom is not a failed deploy: the ArgoCD
Application reports **Healthy** while every data route 503s. Recognize that
combination for what it is before assuming the deploy itself is broken.

---

## Current Services

| Service | Port | Status | Purpose |
|---|---|---|---|
| `weather` | 8000 | Live | First data-source collector (Phase 8's 8A retrofit). Captures forecast-at-kickoff and current conditions per pro football stadium on a cadence, into the shared signal lake. Exposes `/health`, `/metrics`, `/catalog`, `/signals`, `/signals/convergence`, `/refresh` — bearer-token auth on every route except `/health` and `/metrics`; the stadium routes are gone |
| `player-projections` | 8001 | Stub mode | Polls the S3 projections snapshots; returns empty until the generator publishes |

### player-projections — How It Works

Runs in **stub mode** when `PROJECTIONS_SNAPSHOT_URL` is empty (no upstream yet). Returns `{"format":"ppr", "projections":[], "count":0, "last_updated":null, "upstream_healthy":false}`.

**The API — `GET /projections`:**

| Param | Default | Meaning |
|---|---|---|
| `format` | `ppr` | Scoring mode: `standard`, `half-ppr`, or `ppr`. Anything else → 422 |
| `pos` | *(all)* | Optional comma-separated position filter, e.g. `WR` or `RB,WR,TE`. Unknown position → 422 |

`FLEX` is **not** a valid `pos` value — it is a frontend display lane, requested
as `pos=RB,WR,TE`. Asking for `pos=FLEX` returns 422 rather than an empty list,
so a client bug surfaces instead of looking like a quiet week.

The filter is a convenience, not a boundary: a whole format document is ~350
rows (~45 KB), so a client may omit `pos` entirely and slice client-side.

**Upstream architecture:** the projections generator (out of repo) aggregates internal sources —
weather, injury reports, betting lines, news, field type — and writes one
curated projections JSON document per scoring format (standard, half-PPR, PPR)
to S3. `player-projections` polls **all three** documents each interval and
caches them independently, so one corrupt document does not affect the other
two. The document shape is contracted in `contracts/projections-snapshot/` — see
`docs/testing-strategy.md`.

Once the generator begins publishing:
1. Set `PROJECTIONS_SNAPSHOT_URL` in the ConfigMap to the S3 URL **template**, containing
   a `{format}` placeholder — e.g. `https://bucket.s3.amazonaws.com/{format}.json`.
   The service substitutes each scoring mode in turn.
2. Service begins polling every 15 minutes (configurable via `POLL_INTERVAL_SECONDS`)

Each fetch asserts the document's own `format` field matches the one being
polled. A `PROJECTIONS_SNAPSHOT_URL` missing its `{format}` placeholder therefore fails
two of the three formats loudly instead of serving one document as all three.

No API key or Kubernetes Secret needed — S3 auth is handled at the infrastructure level (IAM role on the pod, or a presigned URL baked into `PROJECTIONS_SNAPSHOT_URL`).

Each document's shape is defined by the schema in `contracts/projections-snapshot/` (see
`docs/testing-strategy.md`). Each format's cache is a flat list in upstream
order — not keyed by `id` — and the frontend does the grouping.

---

## Repo Layout

```
services/<name>/        Python service (FastAPI, uv, pyproject.toml)
libs/collector-core/     Shared library for the collector fleet: bearer auth,
                        the five-route contract surface, the capture loop,
                        the append-only S3 lake, the signal envelope, and
                        `app.py`'s `build_collector_app` -- the process
                        wiring (env parsing, lifespan, auth, routes) every
                        collector's `main.py` calls instead of assembling by
                        hand. Every collector (weather is the first) depends
                        on it via the uv workspace; changes here fall under
                        the `weather` CI workflow AND the integration-test gate.
helm/charts/generic-service/    Base Helm chart all services share
helm/values/<name>/     Per-service value overrides
.github/actions/        Composite CI actions: python-lint, python-test, helm-lint, build-push
.github/workflows/      Per-service CI callers (one file per service)
infra/grafana-stack/    Helmfile: OTel Collector, Prometheus, Loki, Tempo, Grafana
infra/kind/             Kind cluster config for local dev
scripts/                deploy-local.py, stack-up.py
docs/                   Architecture, onboarding, service contract
tests/                  Platform tests — things no single service can see: scripts/
                        (rollback, argocd-deploy) and Helm chart render assertions.
                        Per-service tests live in services/<name>/tests/, not here.
                        Run by the `platform-tests` CI job.
contracts/              Committed contracts — see contracts/README.md.
                        projections-snapshot/ = inbound S3 doc schema (hand-written)
                        projections-api/      = outbound response schema (hand-written)
                        openapi/              = API surface snapshots (generated)
                        responses/            = response field-name snapshots (generated)
                        Regenerate the generated two deliberately — CI fails on drift.
```

---

## How CI Works

Each service has one workflow file (`.github/workflows/<service>.yml`) that calls shared composite actions directly. Four parallel/sequential jobs per PR:

- `lint` → `.github/actions/python-lint` (ruff check + format)
- `test` → `.github/actions/python-test` (pytest)
- `helm-lint` → `.github/actions/helm-lint`
- `build-push` → needs all three; runs only on push to main

There is also a **required** `integration-test` check that gate-keeps merges. It spins up a Kind cluster, deploys the full stack, and runs `scripts/smoke-test.sh`. It is **path-filtered**: a `changes` job inspects the PR diff and only runs the heavy test when the **deployable surface** (`services/`, `helm/`, `infra/`, `scripts/`) changed. For docs/CI-only PRs the `integration-test` job is skipped, and a skipped required check counts as a pass — so those PRs merge without spinning up a cluster. There is no label or manual gate: the test simply runs on every PR that touches code it can actually validate, and blocks the merge if it fails. The workflow uses `concurrency` with `cancel-in-progress`, so a new push to a PR cancels that PR's in-flight run rather than spinning a second cluster in parallel.

---

## Adding a New Service

See `docs/onboarding.md`. Short version:
1. `services/<name>/` — FastAPI app, Dockerfile, pyproject.toml, uv.lock
2. `helm/values/<name>/values.yaml`
3. `infra/gitops/envs/local/<name>/values.yaml` — initial image tag (`0.1.0`)
4. `infra/gitops/argo/<name>.yaml` — Argo CD Application manifest (copy from `infra/gitops/argo/weather.yaml`, update name and value paths)
5. Copy `.github/workflows/player-projections.yml` → `.github/workflows/<name>.yml`, update service name in update-gitops-tag job
6. Register in `scripts/deploy-local.py` and `scripts/stack-up.py`

If the service needs secrets: add `extraEnv` to the values file with a `secretKeyRef` (see the Helm Chart — Secret Support section below for the pattern).

### Adding a New Collector

A collector's process wiring is not written by hand — `libs/collector-core/collector_core/app.py`
owns it. `services/weather/` (the first collector) is the reference: its
`main.py` is under 60 lines and contains only the four things that are
genuinely weather's.

1. Build `services/<name>/` per the steps above, depending on `collector-core`
   via the uv workspace (see `services/weather/pyproject.toml`'s
   `[tool.uv.sources]`).
2. Write a `capture(season, week, *, client, lake, now, deadline=None)`
   function returning `dict[str, Envelope]` — one envelope per signal type —
   and a `CollectorMetrics(name)` instance for it to record against.
3. Write a `signal_matches(row, params) -> bool` predicate for whatever
   filters beyond `season`/`week`/`signal_type` the collector's rows support.
4. In `main.py`, build a `CollectorDescriptor` (name, cadence class, signal
   types, supported filters, `capture`, `signal_matches`, `metrics`, and
   optionally `next_event_at`/`setup_telemetry`/`client_factory`) and pass it
   to `build_collector_app`. That call gets you environment parsing
   (`REFRESH_MIN_INTERVAL_SECONDS`, `CAPTURE_DEADLINE_SECONDS`,
   `CAPTURE_SEASON`, `CAPTURE_WEEK`, `CAPTURE_ENABLED`), `CaptureState`,
   `RefreshGate`, the lake writer, the lifespan (capture loop start/stop,
   OTel guard, graceful shutdown), bearer auth, and the standard five routes
   — all without writing any of it again.
5. There is no `auth.py` and no `scheduler.py` wrapper to copy — the shared
   loop and the bearer middleware are mounted by `build_collector_app`
   itself. A collector only writes a `scheduler.py` if it has its own
   `next_event_at` lookup (weather's `next_kickoff` is the model); most
   collectors will not need one.
6. Any route beyond the standard five (weather's `/signals/convergence` is
   the example) is a plain `@app.get`/`@app.post` added to `main.py` after
   the `build_collector_app` call, reaching the lake and collector name via
   `app.state.collector_spec` rather than a module-level global.

---

## Dockerfile Pattern (Canonical)

**Collectors build from the repo root**, not the service directory, because they
depend on the `libs/collector-core/` workspace member by path:

    docker build -f services/<name>/Dockerfile -t <name>:local .

The build stage copies `libs/collector-core/` before `uv sync`, since the lock
cannot resolve without the member present. Services that do not consume the
shared library keep the original service-directory context.

All services use uv's official multi-stage pattern for packaged services:

```dockerfile
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Step 1: deps only (layer cached until uv.lock/pyproject.toml change)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
# Step 2: install package as wheel (--no-editable bakes it into the venv)
COPY pyproject.toml uv.lock ./
COPY <pkg>/ ./<pkg>/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
# Use numeric UID — Kubernetes runAsNonRoot requires a numeric user to verify non-root
RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 65532
ENV PATH="/app/.venv/bin:$PATH"
# No PYTHONPATH needed — --no-editable installs the package as a proper wheel
EXPOSE <port>
CMD ["uvicorn", "<pkg>.main:app", "--host", "0.0.0.0", "--port", "<port>"]
```

---

## OTel Pattern

OTel activates only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (injected by the generic-service Helm chart via ConfigMap). Services run and test cleanly without a collector.

```python
if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from .telemetry import setup_telemetry
    setup_telemetry(app)
```

Each service has a `telemetry.py` with traces (OTLP gRPC → OTel Collector → Tempo) and metrics (`PrometheusMetricReader` → `/metrics` → Prometheus).

**`setup_telemetry` must rebuild the middleware stack.** `setup_telemetry` runs
inside the lifespan handler, and `FastAPIInstrumentor.instrument_app` only
patches `app.build_middleware_stack`. Starlette builds and caches that stack on
its first `__call__` — which *is* the lifespan scope — so the patch arrives too
late and `OpenTelemetryMiddleware` is never installed. Every `telemetry.py`
therefore ends with:

```python
FastAPIInstrumentor.instrument_app(app)
app.middleware_stack = app.build_middleware_stack()
```

Omit that line and the failure is **silent**: `_is_instrumented_by_opentelemetry`
still reads `True`, `/metrics` still works, and the service simply produces no
server spans while its httpx client spans arrive in Tempo with no parent.
`test_server_middleware_is_actually_installed` in each service's
`tests/test_telemetry.py` guards it by walking the real middleware chain —
asserting `instrument_app` was *called* does not catch this.

**Collector service name:** The Helmfile release is named `otel-collector`, but the Helm chart appends `-opentelemetry-collector`, making the in-cluster DNS name `otel-collector-opentelemetry-collector.monitoring.svc.cluster.local`. This is set in `helm/charts/generic-service/values.yaml`. If traces/logs stop flowing while `/metrics` still works, this is the first thing to check — Prometheus scrapes pod annotations directly and is unaffected by a broken OTel endpoint.

**Debugging the pipeline manually:** `kubectl port-forward` to gRPC (4317) is unreliable. Use the HTTP OTLP endpoint (4318) when you need to post test spans/logs:
```bash
kubectl exec <pod> -- python3 -c "
import urllib.request, json
urllib.request.urlopen(urllib.request.Request(
  'http://otel-collector-opentelemetry-collector.monitoring.svc.cluster.local:4318/v1/traces',
  data=json.dumps({'resourceSpans':[...]}).encode(),
  headers={'Content-Type':'application/json'}
))
"
```

---

## Helm Chart — Secret Support

Services that need Kubernetes Secrets use `extraEnv` in their values file:

```yaml
extraEnv:
  - name: MY_SECRET_VALUE
    valueFrom:
      secretKeyRef:
        name: my-k8s-secret
        key: the-key
        optional: true   # allows pod to start before secret exists
```

The `optional: true` flag means a pod deploys even when the Secret hasn't been created yet — important for stub-mode services like `player-projections` before its upstream is live.

---

## Testing

```bash
cd services/<name>
uv run pytest -v
```

```bash
cd libs/collector-core
uv run pytest -v
```

Tests use `respx` to mock `httpx` calls. OTel not initialized in tests. State-based endpoint tests pre-populate the in-memory cache via `app.state.collector_spec.state` directly. `libs/collector-core`'s own suite (`libs/collector-core/tests/`) proves the shared router, capture loop, and `build_collector_app` wiring against a fake collector, independent of `weather`; `services/weather/tests/` pins the same shapes against the real service and additionally validates real capture output against `contracts/signal-envelope/collectors/weather.json`.

---

## Rollback

```bash
python scripts/rollback.py <service> <target-tag>
```

See `docs/runbooks/rollback.md` for the full runbook.

---

## Chaos Engineering

```bash
cd infra/chaos-mesh && helmfile apply          # install (not part of stack-up.py)
uv run --with pyyaml==6.0.3 python scripts/run-chaos.py --list
uv run --with pyyaml==6.0.3 python scripts/run-chaos.py pod-kill
```

Scenarios live in `chaos/scenarios/`, one multi-document YAML each: a
`foundry.chaos/v1` head carrying the steady state, hypothesis, and
Prometheus-checked criteria, followed by the Chaos Mesh resources that inject
the fault.

`scripts/run-chaos.py` reaches Prometheus through the Kubernetes API proxy
(`kubectl get --raw`), never a port-forward. **An empty query result is a hard
error, not a zero** — Prometheus answers identically for a series that has never
existed and for a typo'd metric name, so a check opts in with `allowEmpty: true`
where absence is genuinely correct.

`chaos-test.yml` runs on `workflow_dispatch`, and on `pull_request` for changes
under `chaos/`, `infra/chaos-mesh/`, `scripts/run-chaos.py`, or the workflow
itself. No label, no schedule. **It is not a required check** — chaos scenarios
are timing-sensitive, so it earns trust before it blocks anything.

Coverage is therefore still discontinuous: a regression arriving from outside
those paths is caught by nothing until somebody triggers a run. `platform-tests`
validates scenario files structurally but never executes them, so a criterion
whose PromQL is valid-but-wrong passes it. Accepted deliberately, and stated in
`docs/chaos-runbook.md` rather than quietly patched with a schedule.

See `docs/chaos-runbook.md` for known failure modes, including that a chaos run
exercises `main`'s images rather than your branch's.

---

## Local Stack

```bash
python scripts/stack-up.py                    # Kind cluster + all services + port-forwards
python scripts/stack-up.py --forward-only     # Port-forwards only (skip build/deploy)
python scripts/deploy-local.py <name>         # Redeploy a single service
```

Requires: `kind`, `kubectl`, `helm`, `helmfile`, `docker`.

**When ArgoCD is running:** `deploy-local.py` will fail with a Server-Side Apply conflict because ArgoCD already owns the Deployment fields (`image`, `imagePullPolicy`). Use `--forward-only` when the cluster is already up and you just need to re-bind port-forward tunnels after a restart or session change.

---

## PR Workflow

**Before opening any final PR** (not just a review request — the PR that goes to main), you MUST run the `superpowers:pr-uat` skill. This walks through unit tests, service startup, HTTP endpoints, Docker build, container runtime, Helm render, Helm lint, and CI action reference resolution. Do not skip it.

---

## Phase Status & Tagging

The **README Phases table** is the single source of truth for where the project is; each `docs/architecture/phase-N-*.md` carries a Status banner that echoes it. When a PR completes a phase, flip the doc banner + README table and tag the milestone commit `phase-N` (annotated, then push). Full rules — including why GitOps stays on Git-SHA image tags and when a service graduates to SemVer — are in [`docs/tagging-policy.md`](docs/tagging-policy.md). Milestone tags are documentary and never touch `infra/gitops/`.

---

## ArgoCD / GitOps Behavior

All ArgoCD Applications have `selfHeal: true` and `automated.prune: true`. This means:

- **Any manual `kubectl patch` or `kubectl apply` to a managed resource is reverted within seconds.** Do not try to fix live ConfigMaps, Deployments, or Service objects by hand — the change will disappear before the pod restarts.
- **The Application objects themselves are also managed** (by the app-of-apps), so patching the Application spec (e.g. disabling selfHeal) is also reverted.
- **The only way to make a change stick is to merge it to `main`.** ArgoCD tracks `targetRevision: main` for all apps.

If you need to verify a fix that isn't merged yet, work from inside the cluster (e.g. `kubectl exec` into a pod and exercise the changed code path directly) rather than patching cluster state.

---

## Key Decisions

- **Split lint/test CI jobs** — parallel jobs catch failures independently, standard since ~2023.
- **`--no-editable` Docker** — canonical uv pattern for packages; package dir copied directly into the build stage, wheels go into venv, no PYTHONPATH needed in runtime.
- **Per-service workflow files** — one file to copy per onboard, no reusable workflow indirection.
- **OTel guard on env var** — no collector needed for local dev or tests; Kubernetes injects it.
- **The projections generator lives outside this repo** — the ML/ranking methodology is the product's value and stays out of version control. It publishes snapshots to S3 for `player-projections` to poll, with S3 auth handled at the infrastructure level (see ADR 0002). Foundry never deploys or calls it; a file in a bucket is the whole interface.
- **Stub mode for not-yet-built upstreams** — `PROJECTIONS_SNAPSHOT_URL` empty = service runs, returns empty data, no crashes. Lets the service be deployed and observed before its dependency exists.

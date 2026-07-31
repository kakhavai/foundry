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

**This table is documentation, not registration.** The inventory of collectors
is `contracts/collector-registry.yaml`, and the local/CI tooling reads *that* —
nothing reads this table. A new collector is **not required** to add a row, and
the drift gate deliberately does not ask for one: twenty-four collectors each
editing one markdown table is twenty-four merge conflicts on a file that no
machine consumes. The rows below are the services with a bespoke story worth
reading before you touch them. Add one only when a collector has something
genuinely surprising to say; the registry already carries name, gateway path,
cadence class and signal types for every collector, surprising or not.

| Service | Port | Status | Purpose |
|---|---|---|---|
| `weather` | 8000 | Live | First data-source collector (Phase 8's 8A retrofit). Captures forecast-at-kickoff and current conditions per pro football stadium on a cadence, into the shared signal lake. Exposes `/health`, `/metrics`, `/catalog`, `/signals`, `/signals/convergence`, `/refresh` — bearer-token auth on every route except `/health` and `/metrics`; the stadium routes are gone |
| `player-projections` | 8001 | Stub mode | Polls the S3 projections snapshots; returns empty until the generator publishes |
| `player-identity` | 8002 | Live | Platform collector (Phase 8A). The only collector that decides what a `player_id` is: canonical `fdy-` records with a published-crosswalk `external_ids` block, plus the standing name-resolution miss queue. Exposes the standard five plus `GET /resolve`, `POST /resolve/batch` (≤500), `GET /unresolved`. Deployed with `CAPTURE_ENABLED=false` — the upstream document is ~5 MB and asks for at-most-daily polling, so the loop is off in CI and local clusters |
| `roster-scope` | 8003 | Live (stub resolver) | Platform collector (Phase 8A). Resolves config rules (`QB≤2/RB≤3/WR≤4/TE≤2` per team, plus every kicker and all 32 team defenses) against the live depth chart into a versioned membership list — the 416-slot universe every other collector fetches before pulling anything. Standard five plus `/scope/players`, `/scope/rules`, `/scope/diff`. Depends on `player-identity` conceptually, not in code: an empty `PLAYER_IDENTITY_URL` selects a deterministic stub resolver |

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
                        on it via the uv workspace, so `libs/**` is in every
                        collector's CI path filter; its own lint/test live in
                        collector-core.yml, and it is also covered by the
                        integration-test gate.
helm/charts/generic-service/    Base Helm chart all services share
helm/values/<name>/     Per-service value overrides
.github/actions/        Composite CI actions: python-lint, python-test, helm-lint,
                        build-push, update-gitops-tag, and changed-services --
                        the one that turns a diff into the job matrix, so
                        services.yml needs no per-service file
.github/workflows/      services.yml covers every deployable service as a matrix
                        leg. foundry-cli.yml and collector-core.yml are the two
                        deliberate exceptions (no Helm values, no image, no
                        GitOps entry). There is no per-service workflow file.
infra/grafana-stack/    Helmfile: OTel Collector, Prometheus, Loki, Tempo, Grafana
infra/kind/             Kind cluster config for local dev
scripts/                collectors.py (the fleet list every other script and
                        the integration workflow derives from — reads the
                        registry + each service's Helm values, and is the
                        reason adding a collector edits neither),
                        new-collector.py (the scaffolder: the ONLY supported
                        way to start a collector, gated by
                        tests/test_new_collector.py), then deploy-local.py,
                        stack-up.py, smoke-test.sh, check-registry.py,
                        rollback.py, run-chaos.py, run-load.py,
                        argocd-deploy.py
docs/                   Architecture, onboarding, service contract, and
                        collectors.md — the collector authoring guide
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
                        collector-registry.yaml = the inventory of DEPLOYED collectors
                        (hand-written, append-only) + its .schema.json
```

---

## How CI Works

**One workflow covers every deployable service:** `.github/workflows/services.yml`.
There is no `.github/workflows/<service>.yml` any more — a service is a **matrix
leg**, not a file. The jobs and their order are unchanged:

- `lint` → `.github/actions/python-lint` (ruff check + format)
- `test` → `.github/actions/python-test` (pytest)
- `helm-lint` → `.github/actions/helm-lint`
- `runtime-imports` → every module must import with `--no-dev` installed
- `build-push` → needs all four; runs only on push to main
- `update-gitops-tag` → needs `build-push`; writes the image tag into
  `infra/gitops/envs/local/<name>/values.yaml`

**Per-service path filtering survives the collapse, and that is the whole trick.**
A naive matrix would run twenty-six services' jobs on a one-line change to one
collector. Instead the `changes` job calls `.github/actions/changed-services`,
which generates one `dorny/paths-filter` rule per service from
`contracts/collector-registry.yaml` plus each service's Helm values, and emits
**only the affected services** as the matrix. A PR touching `services/weather/`
produces a one-element matrix, exactly as `weather.yml` used to.

Two consequences worth knowing before you debug a surprising run:

- **An empty matrix vector is a workflow *error* in GitHub Actions, not a skipped
  job**, so every matrix job carries `if: needs.changes.outputs.services != '[]'`.
  Remove that guard and every docs-only PR fails the run.
- **`services.yml` also has a workflow-level `paths:` trigger**, which must stay a
  *superset* of every generated per-service filter. A path filtered by
  `changed-services` but missing from the trigger never reaches the filter,
  because the workflow never starts — and the symptom is not a failure, it is
  that a service's CI silently stops running.
  `tests/test_service_ci_coverage.py::test_workflow_trigger_covers_every_filter_path`
  is what holds the two in step. Its other job is to keep the gitops bot's
  `chore(gitops): update <service> image tag` commits from starting a run.

`services/foundry-cli` and `libs/collector-core` keep their own workflows
(`foundry-cli.yml`, `collector-core.yml`): neither has Helm values, an image or a
GitOps entry, so three of the matrix jobs do not apply. `collector-core.yml` is
where the library's own lint/test live — they used to sit inside `weather.yml`,
which meant the shared library's suite ran under one arbitrary consumer's name
and a second collector wanting the same guarantee had to copy the jobs.

There is also a **required** `integration-test` check that gate-keeps merges. It spins up a Kind cluster, deploys the full stack, and runs `scripts/smoke-test.sh`. It is **path-filtered**: a `changes` job inspects the PR diff and only runs the heavy test when the **deployable surface** (`services/`, `helm/`, `infra/`, `scripts/`) changed. For docs/CI-only PRs the `integration-test` job is skipped, and a skipped required check counts as a pass — so those PRs merge without spinning up a cluster. There is no label or manual gate: the test simply runs on every PR that touches code it can actually validate, and blocks the merge if it fails. The workflow uses `concurrency` with `cancel-in-progress`, so a new push to a PR cancels that PR's in-flight run rather than spinning a second cluster in parallel.

---

## Adding a New Service

**If it is a collector, do not do any of this by hand — run
`scripts/new-collector.py`.** See the next section. This list is what the
scaffolder is doing on your behalf, and what a non-collector service still does
manually.

See `docs/onboarding.md`. Short version:
1. `services/<name>/` — FastAPI app, Dockerfile, pyproject.toml. A **collector**
   is a uv workspace member and shares the root `uv.lock`; the root
   `[tool.uv.workspace]` globs `services/*`, so there is no member list to
   append to — but you must still run `uv lock`, because the lockfile records
   members. A **non-collector** (`player-projections`, `foundry-cli`) is named
   in that table's `exclude` and owns its own `uv.lock`.
2. `helm/values/<name>/values.yaml`
3. `infra/gitops/envs/local/<name>/values.yaml` — initial image tag (`0.1.0`).
   Still needed: the generated Application references it as its second
   valueFile, and a missing one renders as a **failed** Application rather than
   an absent one.
4. **Argo CD: nothing.** `infra/gitops/argo/<name>.yaml` no longer exists for any
   service. `infra/gitops/argo/applicationset.yaml` generates the Applications
   from a git directory generator over `helm/values/*`, so step 2 *is* the
   registration.
5. **CI: nothing.** There is no `.github/workflows/<name>.yml` to copy.
   `.github/workflows/services.yml` picks the service up as a matrix leg.
6. **Collectors: nothing.** The registry entry the scaffolder appends is the
   registration — `scripts/deploy-local.py`,
   `scripts/stack-up.py`, `scripts/smoke-test.sh` and both service-list steps
   of `.github/workflows/integration-test.yml` derive from it via
   `scripts/collectors.py`. A **non-collector** service (there is one,
   `player-projections`) has no registry entry, so it is named in
   `scripts/collectors.py`'s `NON_COLLECTOR_SERVICES`.

If the service needs secrets: add `extraEnv` to the values file with a `secretKeyRef` (see the Helm Chart — Secret Support section below for the pattern).

**The values file is where the tooling reads a service's deployment facts
from** — its port (`service.port`), its bearer-token Secret (whatever
`COLLECTOR_TOKEN`'s `secretKeyRef` names), its lake-credentials Secret
(`AWS_ACCESS_KEY_ID`'s), and `CAPTURE_ENABLED`. None of those are duplicated
into the registry or into a deploy file, on purpose: the values file is the
artifact Kubernetes actually applies, so a second copy could only ever drift
away from it. Concretely, `deploy-local.py` creates *the Secret the pod
references* rather than one named by convention — a values file naming a
Secret the tooling never creates would otherwise give you a pod that starts,
reports Healthy, and 503s on every data route.

### Adding a New Collector

**Run the scaffolder. Do not copy an existing collector.**

```bash
uv run --no-project --with pyyaml==6.0.3 python3 scripts/new-collector.py \
    betting-lines --cadence volatile --signal-types game_line_snapshot
uv lock
cd services/betting-lines && uv run pytest -v
```

[`docs/collectors.md`](docs/collectors.md) is the authoring guide — descriptor
fields, the five-route contract, coverage, the failure envelope, the memory
rules, and what the five generated TODOs actually ask of you. Read it before
writing capture logic. What follows is only what a reader of *this* file needs.

`scripts/new-collector.py` generates `services/<name>/` (package, `main.py`,
`capture.py`, `metrics.py`, `signals.py`, an adapter stub, `Dockerfile`,
`pyproject.toml`, `README.md`, three test files, optional `smoke.sh`),
`helm/values/<name>/values.yaml`, `infra/gitops/envs/local/<name>/values.yaml`,
`contracts/signal-envelope/collectors/<name>.json`, and **one appended registry
entry** — which is the only shared file it touches.

It deliberately generates **no** CI workflow, **no** Argo CD Application, **no**
`telemetry.py`, **no** `auth.py`, **no** `scheduler.py`, and **no** per-member
Dockerfile `COPY` lines, because Wave 0 made every one of those either derived
or shared. Copying a collector by hand is how they grow back, twenty-four times
over.

**If a new collector needs you to edit `deploy-local.py`, `stack-up.py`,
`smoke-test.sh`, `integration-test.yml`, `services.yml`, the root
`pyproject.toml` or the ApplicationSet, that is a bug in the tooling — report
it rather than working around it.** `tests/test_collector_tooling.py` makes it
a test failure, and `tests/test_new_collector.py` scaffolds a collector into a
tmpdir and asserts the result lints, renders through Helm, satisfies the
registry drift gate and reaches every derived list **unmodified**.

Three things that fail *silently* and therefore survive review:

- **`telemetry_module` is a dotted string, never a callable, and you should not
  set it at all.** It defaults to `"collector_core.telemetry"`, the fleet's
  shared wiring, and `build_collector_app` passes your descriptor's `name` as
  the service name. Wave 0 deleted three identical copies of that file; do not
  add a fourth. `build_collector_app` is the only thing that imports it, via
  `importlib.import_module`, and only once `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set. Importing the module at `main.py`'s own top level and handing the
  function in already bound is legal Python and every test stays green — and it
  reintroduces exactly the eager import the guard exists to prevent. That is
  why the field takes a string. Set it to `None` only to disable telemetry, or
  to your own dotted path if the collector genuinely needs more, in which case
  call the shared `setup_telemetry(app, service_name)` rather than forking it.
- **`coverage.expected` must never derive from what succeeded.** A collector
  that builds its expectation from the document it just fetched reports a
  truncated upstream — 100 of 2,900 records — as `expected: 100, present: 100`,
  ratio 1.0. `CoverageAccumulator(floor=...)` is how you avoid it, and the
  scaffolder generates the pattern.
- **`POST /refresh` returns 202 — accepted, not done.** A test that reads
  `/signals` on the next line is a race. It used to be won by accident, because
  nothing in the capture path yielded until the lake write moved to
  `asyncio.to_thread`. Poll with a bounded, loud helper; the scaffolder
  generates one.

A collector writes none of its own coverage floor, error cap, failure envelope
or lake offloading. `collector_core.coverage` supplies `CoverageAccumulator` and
`cap_errors`; `collector_core.failure.fail_capture` writes the `present: 0`
envelope and re-raises; `collector_core.publish.publish_capture` is the
**success** path's tail — write, record coverage, return the envelopes;
`collector_core.lake` supplies `awrite`/`alist_keys`/
`aread`, and the lake you are handed **refuses a synchronous call from the event
loop thread** — boto3 on the loop gates readiness on object-store latency.
`collector_core.streaming.stream_csv_dicts` is how a large CSV upstream is read:
stream and filter as you parse, never hold the response twice.

**An object-store outage costs freshness, not availability — same as an
upstream one.** `publish_capture` records a failed lake write on
`collector_capture_failures_total` and returns the envelopes anyway, so a
capture that succeeded still reaches `/signals`. Nine collectors used to
hand-roll that tail and eight re-raised, which discarded a good capture over a
missing archival copy. `fail_capture` keeps the opposite answer for the
opposite case: there the capture itself failed, and installing `present: 0`
envelopes over the last good ones destroys good data.

**The library owns `collector_capture_failures_total` for a failure that ends a
pass.** Both `fail_capture` and `publish_capture` record it. Do not call
`metrics.capture_failure(exc)` before either — that double-counts. Record it
yourself only for a failure the library cannot see: one bad row, one item's
fetch inside a multi-call pass, or a degraded path that builds its own
envelopes. See [`docs/collectors.md`](docs/collectors.md).

Any route beyond the standard five (weather's `/signals/convergence` is the
example) is a plain `@app.get`/`@app.post` added to `main.py` after the
`build_collector_app` call, reaching the lake and collector name via
`app.state.collector_spec` rather than a module-level global — and it must be
added to `gateway.publicPaths` in the values file, or it 404s at the gateway
while working in-cluster.

Extra routes are also the only reason to pass `--smoke-hook`, which generates
`services/<name>/smoke.sh`. `scripts/smoke-test.sh` runs it after the standard
contract surface passes, with `SMOKE_COLLECTOR`, `SMOKE_BASE_URL` (direct to
the Service), `SMOKE_GATEWAY_URL`, `SMOKE_TOKEN` and `SMOKE_CAPTURE_ENABLED` in
the environment; `services/weather/smoke.sh` is the model. A collector with no
extra routes writes no file — the standard surface is asserted for every
registered collector automatically, so `scripts/smoke-test.sh` is never edited.
**Do not POST `/refresh` from a hook when the collector runs with
`CAPTURE_ENABLED=false`**: a dispatched refresh reaches the upstream regardless
of that flag, so it would hit a third party on every PR.

---

## The Collector Registry

`contracts/collector-registry.yaml` is the inventory of collectors, and the
projections generator reads it to decide what to call. It lists **deployed**
collectors only. The twenty-six-collector staging table in
`docs/architecture/phase-8-data-source-collectors.md` is the *plan*; this file
is the *inventory*. That is not a stylistic split — pre-listing unbuilt
collectors would red the "every entry has a deployed collector" gate on day
one, and the only fix would be to weaken it into nothing.

Two consequences, both mechanical rather than conventional:

- an entry lands in the **same PR** as its service, and
- **after** the PRs that added its `depends_on` entries.

The file is **append-only**. Entries are in merge order, not sorted, and
nothing in the gate requires sorting, grouping, or regeneration — so two
collectors added on two branches merge as a plain append. Do not reorder or
reformat existing entries.

**The drift gate runs in two places.** Both matter; neither subsumes the other:

| | Where | What it can see |
|---|---|---|
| `tests/test_collector_registry.py` | `platform-tests`, no cluster | schema, uniqueness, `services/<name>/` exists, Helm `gateway.pathPrefix` == `path`, Argo + GitOps manifests exist, both reverse directions, `depends_on` resolution / self-dependency / cycles, and the entry vs. the service's own `CollectorDescriptor` **read by AST** |
| `tests/test_collector_tooling.py` | `platform-tests`, no cluster | that the *tooling* derives from the registry: a synthetic entry in a tmpdir reaches every derived list, and **no collector name appears literally** in `deploy-local.py`, `stack-up.py`, `smoke-test.sh` or `integration-test.yml` |
| `scripts/check-registry.py` | `scripts/smoke-test.sh`, inside the required `integration-test` | each collector's **live** `GET /catalog`, fetched through the gateway |

**Why AST and not an import:** `platform-tests` installs only pytest, pyyaml
and jsonschema. Importing `services/weather/weather/main.py` would pull in
fastapi, httpx and prometheus_client, none of which are there. The test parses
the service tree instead, builds a module-level constant table, and resolves
the descriptor's kwargs through it — `CadenceClass.VOLATILE` resolves via enum
members read out of `collector_core/cadence.py`, so adding a cadence class
cannot drift. **A registered collector with no discoverable
`CollectorDescriptor` is a hard failure, never a skip.**

There is deliberately **no committed `/catalog` fixture**. A fixture is a copy
of the answer, and a copy of the answer cannot detect that the answer changed.

**One known gap, stated rather than implied:** `scope_aware` is type-checked
as a bool and nothing else. No code representation of it exists today, so its
correctness is human-reviewed.

**`envelope_version` is a string** — `"1"`, quoted, in the registry. This was
an open int-vs-string inconsistency and has been **settled**: everything else
in the repo already used the string (`collector_core.envelope.ENVELOPE_VERSION`,
`contracts/signal-envelope/envelope.v1.schema.json`'s `{"const": "1"}`, every
committed fixture, `GET /catalog`'s response, and the lake path segment `v1`).
The registry was the sole outlier, and only because one line of the phase doc
wrote it unquoted. Both gates — `tests/test_collector_registry.py` and
`scripts/check-registry.py` — now compare **exactly**; the old
`str()`-on-both-sides comparison was a workaround for the undecided type, and
it would also have passed `1` against `"1.0"`.

`GET /collectors` — the phase doc has the gateway serving the registry live —
is **not built**. See the follow-up notes in the Phase 8A PR: it needs a
generated JSON copy of the file, a generator with *its own* drift gate, a
GitOps home, and an auth decision (a direct response never reaches a service,
so it would be unauthenticated by construction). The generator reads the
committed file from the repo meanwhile, which is the spec's own fallback.

---

## Dockerfile Pattern (Canonical)

**Collectors build from the repo root**, not the service directory, because they
depend on the `libs/collector-core/` workspace member by path:

    docker build -f services/<name>/Dockerfile -t <name>:local .

`player-projections` is **not** a workspace member, owns its own `uv.lock`, and
still builds from its own directory as the context. Do not use it as a collector
template — `services/weather/Dockerfile` is the collector reference.

**Every collector opens with the shared `workspace-manifests` stage, and it is
not optional.** `uv sync --locked --package <x>` resolves the ENTIRE workspace
graph before it can sync any single member, so every member's `pyproject.toml`
must be in the build context — including members the service does not depend on.
Listing them one `COPY` line at a time is quadratic (26 lines in each of 26
Dockerfiles), and the line you forget breaks an **unrelated** service's image
with `the lockfile needs to be updated`. **No pytest run can see that break**,
because pytest never touches a Dockerfile — adding `roster-scope` broke
`services/weather/Dockerfile` exactly this way.

The stage names no member, so adding a workspace member requires no Dockerfile
edit anywhere. `tests/test_dockerfile_workspace.py` fails the build if a
collector loses the stage or reintroduces a per-member `COPY`.

Note `COPY services/*/pyproject.toml ./services/` does **not** work as a
shortcut: Docker flattens a multi-source `COPY` into the destination directory.

```dockerfile
FROM python:3.12-slim AS workspace-manifests
WORKDIR /src
COPY . .
RUN set -eu; \
    mkdir -p /manifests; \
    cp pyproject.toml uv.lock /manifests/; \
    cp -a libs /manifests/libs; \
    find services -mindepth 2 -maxdepth 2 -name pyproject.toml \
        -exec cp --parents {} /manifests/ \;

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Step 1: deps only. BuildKit keys `COPY --from` on the CONTENT it copies, so
# this layer survives a service source edit and busts on a manifest/lock change.
COPY --from=workspace-manifests /manifests/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --package <name>
# Step 2: install package as wheel (--no-editable bakes it into the venv).
# --reinstall-package is REQUIRED: the version never changes (0.1.0), so uv's
# build cache can serve a previously built wheel for changed source and the
# image silently ships stale code. Observed twice on roster-scope.
COPY services/<name>/<pkg>/ ./services/<name>/<pkg>/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package <name> \
        --reinstall-package <name>

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

**Collectors do not write a `telemetry.py`.** `collector_core/telemetry.py` is
the fleet's single copy — traces (OTLP gRPC → OTel Collector → Tempo) and
metrics (`PrometheusMetricReader` → `/metrics` → Prometheus).
`CollectorDescriptor.telemetry_module` defaults to `"collector_core.telemetry"`,
still a dotted string resolved by `importlib` *inside* the
`OTEL_EXPORTER_OTLP_ENDPOINT` guard, and `build_collector_app` passes the
descriptor's own `name` so the resource is named from data rather than a
per-service literal. `OTEL_SERVICE_NAME` still overrides it.

Wave 0 consolidated this: every collector's `telemetry.py` was the same forty
lines differing by exactly one string. Set `telemetry_module=None` to disable
telemetry, or point it at your own module — which takes `(app, service_name)`
and should call the shared `setup_telemetry` first rather than forking it.
`player-projections` is not a collector and keeps its own copy.

**`setup_telemetry` must rebuild the middleware stack.** `setup_telemetry` runs
inside the lifespan handler, and `FastAPIInstrumentor.instrument_app` only
patches `app.build_middleware_stack`. Starlette builds and caches that stack on
its first `__call__` — which *is* the lifespan scope — so the patch arrives too
late and `OpenTelemetryMiddleware` is never installed. The shared module
therefore ends with:

```python
FastAPIInstrumentor.instrument_app(app)
app.middleware_stack = app.build_middleware_stack()
```

Omit that line and the failure is **silent**: `_is_instrumented_by_opentelemetry`
still reads `True`, `/metrics` still works, and the service simply produces no
server spans while its httpx client spans arrive in Tempo with no parent.
`test_server_middleware_is_actually_installed` in
`libs/collector-core/tests/test_telemetry.py` guards it by walking the real
middleware chain — asserting `instrument_app` was *called* does not catch this.
Holding that test once instead of twenty-six times is the point of the
consolidation: it is one chance to drop the line, not twenty-six.

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

## Load and Scale Testing

```bash
uv run --with pyyaml==6.0.3 python scripts/run-load.py --list
uv run --with pyyaml==6.0.3 python scripts/run-load.py ramp
uv run --with pyyaml==6.0.3 python scripts/run-load.py --all --soak-minutes 30
```

k6 runs **in-cluster as a Job**, built from a ConfigMap of `tests/load/*.js` and
hitting `http://player-projections:8001` over ClusterIP. There is no NodePort
path to `player-projections` — it is deliberately not routed through the
gateway — and an in-cluster Job needs no k6 binary on the CI runner or on a
developer's machine.

**Only `player-projections` is covered, in stub mode.** `weather` was excluded
because it was then a synchronous proxy to a rate-limited free API — one ramp
would have exceeded Open-Meteo's daily budget. **8A has since removed that
blocker**: `weather` now captures on a cadence and serves from memory, so
covering it needs a k6 script against its `/signals` surface, not a waiver. It
is the immediate follow-up. See
[`docs/scale-baselines.md`](docs/scale-baselines.md) for every number's
conditions and the caveats that go with them.

**k6 exits `99` when a threshold with `abortOnFail` is crossed.** Only the
`breakpoint` shape treats that as success, and only that code — any other
non-zero is a broken script or an unreachable target, not a measurement.

**The >20% P95 regression gate is not implemented.** Stub-mode numbers would make
it fire on noise. `load-test.yml` is `workflow_dispatch` only and is **not** a
required check.

---

## Local Stack

```bash
python scripts/stack-up.py                    # Kind cluster + all services + port-forwards
python scripts/stack-up.py --forward-only     # Port-forwards only (skip build/deploy)
python scripts/deploy-local.py <name>         # Redeploy a single service
```

Requires: `kind`, `kubectl`, `helm`, `helmfile`, `docker`, **and PyYAML on the
interpreter you run these with** — both scripts derive their service list from
`contracts/collector-registry.yaml` via `scripts/collectors.py`. If your
`python` has no PyYAML, or you are on the CI runner image (which ships a bare
`python3`), use the form CI uses:

```bash
uv run --no-project --with pyyaml==6.0.3 python3 scripts/deploy-local.py <name>
```

`--no-project` keeps `uv` from building the whole workspace to run a script
that imports none of it.

**When ArgoCD is running:** `deploy-local.py` will fail with a Server-Side Apply conflict because ArgoCD already owns the Deployment fields (`image`, `imagePullPolicy`). Use `--forward-only` when the cluster is already up and you just need to re-bind port-forward tunnels after a restart or session change.

---

## Local Environment Gotchas (Windows)

These have each cost real debugging time more than once. They are environment
facts, not preferences.

**Git Bash mangles paths containing `:`.** MSYS path conversion rewrites
`git cat-file -e origin/main:some/file` into `origin\main;some\file`, and the
command then reports the file as missing. This produced a false "the merge
dropped a file" alarm during Phase 5B. Use `git ls-tree` for existence checks,
or set `MSYS_NO_PATHCONV=1`. The same conversion breaks `docker run -v` with
container-absolute paths (`-v "$PWD/x:/scripts"`).

**`python` and `python3` are Windows binaries and resolve `/tmp` to `C:\tmp`,
which does not exist.** A shell heredoc can write to `/tmp` happily and the
Python that reads it back will not find the file. Use an explicit Windows path.

**`jq` is not installed.** `gh ... --json | jq` fails silently inside a loop.
Use `python3 -m json.tool`, or `gh`'s own `--jq` flag, which is built in.

**Argo CD's application controller is a `StatefulSet`, not a Deployment.**
`kubectl scale deploy/argocd-application-controller -n argocd` fails with
NotFound. Scale the StatefulSet if you need Argo to stop reconciling briefly —
and scale it back, since the cluster is shared between concurrent sessions.

**Do not background a wait and return before it finishes.** Long operations
(a cluster build, a load shape, a chaos scenario) must be blocked on in the
foreground. Agents that spawn a background sleep and return have stalled here
repeatedly, and the stall is invisible until the wall-clock budget is gone.

**`kubectl port-forward` survives `pkill -f port-forward` on Windows** — that
matches the shell wrapper, not `kubectl.exe`. Use `Get-CimInstance Win32_Process`
plus `Stop-Process`, or avoid port-forwards entirely: `kubectl get --raw` reaches
Prometheus through the API proxy and behaves identically on Windows and in CI.

---

## PR Workflow

**Before opening any final PR** (not just a review request — the PR that goes to main), you MUST run the `superpowers:pr-uat` skill. This walks through unit tests, service startup, HTTP endpoints, Docker build, container runtime, Helm render, Helm lint, and CI action reference resolution. Do not skip it.

---

## Phase Status & Tagging

The **README Phases table** is the single source of truth for where the project is; each `docs/architecture/phase-N-*.md` carries a Status banner that echoes it. When a PR completes a phase, flip the doc banner + README table and tag the milestone commit `phase-N` (annotated, then push). Full rules — including why GitOps stays on Git-SHA image tags and when a service graduates to SemVer — are in [`docs/tagging-policy.md`](docs/tagging-policy.md). Milestone tags are documentary and never touch `infra/gitops/`.

---

## ArgoCD / GitOps Behavior

**The Applications are generated, not written.** `infra/gitops/argo/` holds two
files: `app-of-apps.yaml` and `applicationset.yaml`. The ApplicationSet's git
directory generator globs `helm/values/*`, so every service with a Helm values
directory gets an Application whose spec is identical to the hand-written ones
that preceded it — same `project`, `source`, `destination`, `syncPolicy` and
finalizer. Two things the controller adds that a hand-written manifest did not
have: an `ownerReference` back to the ApplicationSet, and the label
`argocd.argoproj.io/application-set-name`. The ownerReference is the one with
teeth — **deleting `applicationset.yaml` deletes every Application it
generated**, where deleting one old manifest deleted one app.

**`local` is the only environment that has Applications, so
`argocd-deploy.py promote --to staging|prod` refuses.** The ApplicationSet's
template hard-codes `/infra/gitops/envs/local/<service>/values.yaml` and
`namespace: default`, and `infra/gitops/envs/staging/` and `envs/prod/` hold a
`.gitkeep` and nothing else. `promote` used to copy a per-service
`infra/gitops/argo/<service>.yaml` into `<service>-<env>.yaml`; Wave 0 deleted
those manifests, so it started dying on "source manifest not found" — which
reads as a forgotten file rather than a deliberate deletion. It now refuses
before writing anything and says why. **Do not fix it by re-adding the copy:**
`tests/test_service_ci_coverage.py` fails on a third file in that directory,
the app-of-apps would apply the written manifest into the same namespace the
local Application already owns, and its HTTPRoute would claim the same
`gateway.pathPrefix` — Gateway API breaks that tie on creation timestamp, so it
would apply cleanly and misroute silently. Real promotion needs the
ApplicationSet to become env-aware plus an env strategy (cluster vs namespace,
per-env gateway paths, per-env Secrets) that is Phase 6's work.

All ArgoCD Applications have `selfHeal: true` and `automated.prune: true`. This means:

- **Any manual `kubectl patch` or `kubectl apply` to a managed resource is reverted within seconds.** Do not try to fix live ConfigMaps, Deployments, or Service objects by hand — the change will disappear before the pod restarts.
- **The Application objects themselves are also managed** (by the app-of-apps), so patching the Application spec (e.g. disabling selfHeal) is also reverted.
- **The only way to make a change stick is to merge it to `main`.** ArgoCD tracks `targetRevision: main` for all apps.

If you need to verify a fix that isn't merged yet, work from inside the cluster (e.g. `kubectl exec` into a pod and exercise the changed code path directly) rather than patching cluster state.

---

## Key Decisions

- **Split lint/test CI jobs** — parallel jobs catch failures independently, standard since ~2023.
- **`--no-editable` Docker** — canonical uv pattern for packages; package dir copied directly into the build stage, wheels go into venv, no PYTHONPATH needed in runtime.
- **One matrix workflow, not per-service workflow files** — **reverses an earlier
  decision**, deliberately. The original entry read *"per-service workflow files —
  one file to copy per onboard, no reusable workflow indirection"*, and it was
  right at two services: one file to copy, nothing to understand. It inverts at
  twenty-six. Each file was ~145 lines of which ~6 were the service name, so the
  fleet implied ~3,700 lines that all had to agree, and a CI fix became
  twenty-six edits whose failure mode was silent — miss one and that service
  quietly keeps the old behaviour. The four that existed had **already** drifted:
  `weather.yml` carried the collector-core jobs, `roster-scope.yml` had a
  `runtime-imports` check nothing else had, and the other two had neither. So
  `.github/workflows/services.yml` runs every deployable service as a matrix leg,
  and `.github/actions/changed-services` computes that matrix from the diff so a
  PR still runs only the services it touches. A reusable `workflow_call` was
  rejected as the smaller win: it shrinks the per-service file to ~10 lines but
  keeps twenty-six of them, and keeps onboarding a copy-a-file step. `foundry-cli`
  and `libs/collector-core` stay on their own workflows — neither has Helm values,
  an image or a GitOps entry, so three of the five matrix jobs do not apply.
- **A scaffolder, not a reference collector to copy** — `scripts/new-collector.py`
  generates a collector; `docs/collectors.md` explains it. "Copy `weather` and
  change the names" was fine at one collector and is the mechanism by which the
  fleet re-acquires every file Wave 0 deleted — twenty-four times, each one
  already buried in a service tree somebody then edits on top of. The
  scaffolder's value is not saving typing, it is that **what it does not
  generate is enforceable**: `tests/test_new_collector.py` asserts the exact
  file set both directions, so an extra per-collector copy of something shared
  reds a required check instead of merging. It also bakes in the two things
  that fail silently — the declared coverage floor and the bounded poll after a
  202 `/refresh` — because those are precisely what a copy-paste author drops.
  The root `[tool.uv.workspace]` moved to a `services/*` glob with an explicit
  `exclude` for the same reason: an enumerated member list would be a
  twenty-fourth append to a shared file that no machine reads.
- **OTel guard on env var** — no collector needed for local dev or tests; Kubernetes injects it.
- **The projections generator lives outside this repo** — the ML/ranking methodology is the product's value and stays out of version control. It publishes snapshots to S3 for `player-projections` to poll, with S3 auth handled at the infrastructure level (see ADR 0002). Foundry never deploys or calls it; a file in a bucket is the whole interface.
- **Stub mode for not-yet-built upstreams** — `PROJECTIONS_SNAPSHOT_URL` empty = service runs, returns empty data, no crashes. Lets the service be deployed and observed before its dependency exists.

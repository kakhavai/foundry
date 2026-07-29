# Phase 5 — Resilience Testing & AI Agent Adversarial Layer

> **Status:** 🚧 **In progress** — Stage 1 (rigorous service and platform testing) delivered; Stages 2 and 3 not started · [roadmap](../../README.md#phases)

**Goal:** Prove the platform survives real-world conditions — not just happy-path deployments. Introduce rigorous service and platform-level testing in three escalating stages: exhaustive automated testing, chaos and scale validation, and finally an AI agent adversarial layer where synthetic engineers are released against the platform to test whether it can detect, recover, and iterate under realistic failure modes.

---

## Overview

Phases 1–4 prove the platform works. Phase 5 proves it holds under pressure.

The three stages build on each other:

1. **Rigorous Testing** — close the gap between "tests exist" and "tests cover what matters." Coverage thresholds, contract testing, property-based testing, and load baselines.
2. **Chaos + Scale Testing** — inject infrastructure-level failures and drive the platform beyond its designed load envelope. Find the breaking points before production does.
3. **AI Agent Adversarial Layer** — release AI agents acting as realistic team members (developer, devops engineer, designer) that make plausible but intentionally flawed contributions. The platform must detect degradation, surface it via the Phase 4 detection engine's `EvidenceBundle` and the 4B triage narrator, and support rollback and iteration — all without human intervention in the detection loop.

---

## Stage 1 — Rigorous Service and Platform Testing

### What Gets Built

**Coverage enforcement.** `pyproject.toml` gains `[tool.pytest.ini_options]` with `--cov`, `--cov-fail-under=80`, and `--cov-branch`. PRs that drop coverage below threshold fail CI. Coverage is reported to the GitHub Actions job summary on every run, including failed ones — not a per-PR comment bot (see Deliverables below for why).

**Contract testing (schema-first).** Rather than Pact's consumer-driven approach, contracts are enforced provider-side with committed schemas: JSON Schema for the projections snapshot documents (`contracts/projections-snapshot/`), and committed OpenAPI snapshots for `weather` and `player-projections` (`contracts/openapi/`) with CI failing on undeclared divergence. See [ADR 0002](../adr/0002-provider-driven-contracts.md) for why.

**Property-based testing (Hypothesis).** Services that parse external data (S3 projection payloads, weather API responses) gain Hypothesis tests that generate structurally valid but adversarial inputs: missing fields, wrong types, empty arrays, extremely large payloads. These tests are added to the standard `pytest` suite and run on every PR.

**Service integration tests.** A dedicated `tests/integration/` suite spins up each service with a real HTTP client (no mocks) and exercises edge cases: concurrent requests, upstream timeouts, malformed responses. These run in the integration test job alongside the Kind cluster smoke tests.

### Deliverables

- Updated `pyproject.toml` per service with coverage thresholds and branch coverage
- `contracts/projections-snapshot/` — one JSON Schema covering the snapshot documents for all three scoring formats
- `contracts/openapi/` — committed OpenAPI snapshots with CI divergence detection
- `services/*/tests/test_properties.py` — Hypothesis suites for all external data parsers
- `services/*/tests/integration/` — real HTTP integration tests per service
- `tests/test_helm_otel_endpoint.py` — Helm render assertion for the collector DNS name
- `.github/workflows/foundry-cli.yml` — CI for the triage engine (previously untested)
- Coverage reported to the GitHub Actions job summary — **supersedes** the
  originally specified `.github/actions/coverage-report/` composite action;
  enforcement is handled by `--cov-fail-under`, so the action would have added a
  workflow permission and base-branch bookkeeping for reporting alone
- `docs/testing-strategy.md` — what is tested at each layer and why
- `docs/adr/0002-provider-driven-contracts.md` — why schema-first, not Pact

---

## Stage 2 — Collector Platform, Chaos Engineering and Scale Testing

Stage 2 carries two bodies of work. Chaos testing was specified first, but the
platform had almost nothing to disrupt: `player-projections` polls an S3 URL
that does not exist yet, and `weather` feeds nothing. Building the collector
platform first gives the chaos scenarios real credentialed service-to-service
traffic to break, rather than partitioning a service from an upstream that was
never reachable.

### What Gets Built — collector platform

**One gateway, path-routed.** A single ingress hostname and TLS certificate;
each collector is served under `/collectors/<name>/`. Adding a collector is one
path rule plus one Secret — not a new hostname, DNS record, or certificate. See
CLAUDE.md for why this over per-service hostnames or a registry service.

**Bearer-token auth per collector.** The projections generator runs outside the
cluster and calls in. Each collector reads a token from a Kubernetes Secret via
the existing `extraEnv` + `secretKeyRef` pattern and rejects unauthenticated
requests. Chosen over mTLS and OIDC because the generator is a single external
client on a known machine; the tradeoff is recorded in CLAUDE.md.

**`weather` becomes the first collector.** It keeps its current API and gains
the gateway path plus token enforcement, proving the pattern end to end before a
second collector exists.

Auth is a new failure surface, so it needs its own tests: a missing token, a
wrong token, and an expired/rotated token must all be rejected with the right
status, and the rejection must be observable — not a silent 500.

**Rename `weather`'s metrics to the fleet convention in the gateway PR.**
[Phase 8](phase-8-data-source-collectors.md) specifies fleet-wide names that the
failure-path metrics PR predates and therefore does not follow:

| Shipped | Phase 8 convention |
|---|---|
| `weather_upstream_failures_total{reason}` | `collector_capture_failures_total{collector,reason}` |
| `weather_upstream_requests_total` | folded into the same `{collector}` dimension |

Do it while `weather` is the only collector — the same rename across the
twenty-six-collector catalog is a far worse afternoon. `player-projections` is
deliberately **not** included: it consumes the generator's output rather than
capturing a signal, so it is not part of the collector fleet and its
`upstream_*` names stay as they are.

Note also that Phase 8's 8A **rebuilds** `weather` onto the capture model —
forecast-at-kickoff, polled on a cadence and served from memory — rather than
extending the current stateless proxy. The counters added here instrument the
synchronous request path, which that retrofit removes. They are correct and
load-bearing for the chaos scenarios below, but for `weather` they are
transitional; the chaos scenario that exercises its upstream must be written
against whichever shape is live when it runs.

**Decisions taken in the gateway PR.**

| Question | Decision |
|---|---|
| Gateway component | Gateway API, implemented by Envoy Gateway. `ingress-nginx` is dominated — it still needs a controller and an annotation vocabulary, so it is not the simple option, and the project is winding down, so it is not the durable one. A plain nginx pod is genuinely simpler but makes every collector edit one shared config rather than shipping its own route. |
| Auth enforcement | In each service. The gateway routes and terminates TLS; it does not authenticate. |
| `chaos-test.yml` trigger | `workflow_dispatch` only. No label — CLAUDE.md's no-manual-gate rule governs required merge checks, and a label additionally needs a human to remember it. No nightly schedule either: **chaos coverage therefore exists only when somebody runs it, and will decay silently between sessions.** Accepted, not overlooked. |

Full rationale, including the rejected alternatives (`ingress-nginx`, Traefik, a
hand-rolled nginx pod) and why the AWS-native Gateway API implementations were
not candidates — none of them run on Kind — is in the pull request that
delivered this stage.

### What Gets Built — chaos and scale

**Chaos engineering with Chaos Mesh.** Chaos Mesh is deployed to the Kind cluster. A `chaos/` directory contains scenario manifests:
- `pod-kill.yaml` — kills a random service pod, validates auto-recovery within N seconds
- `network-partition.yaml` — **as originally written this scenario cannot fail.** It
  claimed to validate that `player-projections`' "stub-mode fallback activates."
  Stub mode is not a fallback — it is the permanent state while the generator is
  unbuilt, so blocking outbound calls to an upstream that is never called changes
  nothing observable. Rewrite it against a failure the platform can actually
  exhibit: once the collector gateway lands, partition the gateway or revoke a
  collector's token and assert the failure surfaces in metrics rather than
  silently
- `resource-pressure.yaml` — injects CPU and memory pressure, validates resource limits hold
- `latency-injection.yaml` — **the originally specified +2s cannot trip `weather`'s
  10s upstream timeout** (`weather/main.py:37`, `:61`). Either inject above the
  timeout or revisit the timeout itself — a live request-path change, so it does
  not belong in the observability PR that precedes this work
- `bad-deploy.yaml` — deploys an image that crashes at startup, validates Argo CD health check fails the rollout and the previous version stays live

Each scenario has a defined **steady state** (what "healthy" looks like before), a **hypothesis** (what the platform should do under the failure), and a **pass/fail criterion** checked via Prometheus queries after the scenario runs.

A scenario that cannot fail is worse than a missing one: it reports green and is
counted as coverage. Both corrections above were found by reading the code rather
than by running the scenarios, which is the only reason they did not ship as
passing.

**Load and scale testing with k6.** `tests/load/` contains k6 scripts for each service:
- Ramp test: 0 → 100 RPS over 5 minutes, measure P95 latency and error rate
- Soak test: 50 RPS sustained for 30 minutes, detect memory leaks or connection pool exhaustion
- Spike test: 10x normal load for 60 seconds, validate graceful degradation
- Breakpoint test: ramp until error rate exceeds 1%, document the service's failure threshold

Load test results are published as artifacts and compared against a documented baseline.

**The regression gate is deliberately deferred.** Regressions (P95 latency
increasing >20% vs. baseline) were specified to fail CI, but that gate cannot be
turned on yet. `player-projections` serves `{"projections": [], "count": 0}` in
stub mode, so a ramp against it measures uvicorn's overhead rather than the
service's: real documents are ~350 rows / ~45 KB, where serialization dominates
P95. Baselines captured now are invalidated the day the generator publishes, and
a gate built on them fires on noise until somebody disables it — at which point
the gate is worse than absent.

So `docs/scale-baselines.md` records **stub-mode reference numbers, explicitly
marked invalid once real documents flow**, and the >20% gate turns on when there
is real data behind it. The k6 harness, scripts, and CI wiring are built now;
only the gate waits.

**Chaos + scale CI job.** A new workflow `chaos-test.yml` runs on the `ready-for-chaos` label (separate from `ready-for-merge`). Spins up the Kind cluster, runs the full chaos suite, then runs load tests. Results published to PR as a structured report: scenario name, hypothesis, observed behavior, pass/fail.

**Failure-path metrics come first — delivered.** `_poll_loop`'s bare
`except Exception` emitted nothing: no metric, no cause, no staleness bound.
That blocked the chaos work, because a scenario's pass/fail criterion is a
Prometheus query and a failure mode that emits nothing cannot supply one.

`weather` had the same defect and was pulled into the same PR: `/weather/stadiums`
swallows per-stadium failures and returns 200 with `count: 30` whether thirty
stadiums resolved or zero did, which is what `smoke-test.sh` asserts. The
`latency-injection` scenario had no measurable criterion without this.

```
upstream_poll_failures_total{format, reason}
upstream_cache_age_seconds{format}
upstream_healthy{format}
collector_capture_requests_total{collector}
collector_capture_failures_total{collector, reason}
collector_auth_failures_total{collector, reason}
```

`reason` is one of `http_status`, `timeout`, `transport`, `malformed`, or
`unknown`. A format mismatch reports `malformed`: distinguishing it would have
meant a new exception subclass, and the same person owns the producer and the
consumer, so the exception message already carries the detail.

Metrics only, deliberately. Structured logging is a separate platform-wide
decision (plain vs JSON, OTel log bridge or not) that nothing forces yet — no
service logs anything today — and chaos criteria need metrics, not prose.

### Deliverables

- Gateway ingress with path routing for collectors, plus per-collector bearer-token auth
- `weather` exposed as the first collector, with auth-rejection tests
- Failure-path metrics on `player-projections`' poll loop and `weather`'s
  upstream calls, classified by `reason`
- `infra/chaos-mesh/` — Chaos Mesh helmfile installation
- `chaos/scenarios/` — Chaos Mesh scenario manifests with documented hypotheses
- `tests/load/` — k6 scripts per service
- `.github/workflows/chaos-test.yml` — **decided: `workflow_dispatch` only** — see the decisions table above.
- `docs/chaos-runbook.md` — how to run scenarios manually, how to read results, known failure modes
- `docs/scale-baselines.md` — documented performance baselines per service, updated after each phase

---

## Stage 3 — AI Agent Adversarial Layer

### Overview

The platform is tested not just against infrastructure failures but against realistic human failures — specifically, the kinds of mistakes real team members make. AI agents act as:

- **Developer agent** — opens PRs with plausible but flawed code changes
- **DevOps agent** — modifies infrastructure configuration in ways that degrade reliability
- **Designer/product agent** — drives usage patterns and feature requests that stress the platform in unexpected ways

The hypothesis: if the platform's detection, alerting, incident triage, and rollback machinery works correctly, it should surface and contain any degradation regardless of whether the failure came from a chaos scenario or a bad PR.

### Agent Roles

**Developer Agent.** Given the service contract (`docs/service-contract.md`) and a feature request, the developer agent writes code changes and opens a PR against the repo. Changes are realistic but include deliberate flaws chosen from a fault catalog:
- Memory leak (unbounded list growth in a long-lived process)
- Missing error handling on an external call (crash on upstream 5xx)
- Breaking API contract (response field renamed, schema changed)
- Performance regression (synchronous call in an async handler)
- Dependency upgrade that introduces a breaking change

The agent does not know it is introducing a fault. It is given a feature request and generates the best code it can. The flaw is injected via the scenario definition, not the agent's intent — this keeps the agent behavior realistic.

**DevOps Agent.** Given a platform task (scale up resources, tune a probe, update a config value), the DevOps agent modifies Helm values, CI workflow files, or Dockerfiles and opens a PR. Fault scenarios include:
- Resource limits set too low (OOMKill under normal load)
- Liveness probe too aggressive (pod restart loop under slow startup)
- Incorrect port in service definition (routing breaks silently)
- CI job permissions widened unnecessarily (security regression)
- GitOps tag update broken (deploy loop or no-op)

**Designer/Product Agent.** The designer agent does not write code — it drives the platform by generating load patterns that simulate realistic product usage:
- Gradual traffic growth simulating a product launch
- Bursty access patterns simulating viral content
- Repeated calls to stub endpoints that will be populated by the projections generator
- Requests for new endpoints that do not exist yet (404 rate increase)

The designer agent's pressure is continuous throughout the adversarial test run, not isolated to individual scenarios.

### Test Execution Model

Each adversarial test run is a **scenario session**:

1. **Setup** — cluster running, all services healthy, baselines recorded
2. **Agent activation** — one or more agents are given tasks and begin making changes
3. **Platform observation** — changes flow through CI, deploy via GitOps, observability captures state
4. **Degradation window** — if a fault reaches production, the observation layer detects it
5. **Triage** — `foundry triage` is invoked automatically when error rate exceeds threshold; the Phase 4A detection engine produces an `EvidenceBundle` and the 4B narrator explains it
6. **Recovery** — rollback or fix is applied; platform returns to steady state
7. **Session report** — structured output: what changed, what broke, what the triage engine surfaced (the `EvidenceBundle` and narrator narrative), how long recovery took, what the platform missed

**Pass criteria:**
- Faults that fail CI (lint, test, helm-lint) must be caught before merge
- Faults that pass CI but degrade production must be detected within N minutes
- Phase 4 detection engine must correctly identify the deploy or config change as the likely cause in the `EvidenceBundle`; the triage narrator must surface it accurately
- Rollback must restore steady state within N minutes
- Platform must reach healthy state before the next scenario begins

**Failure criteria (platform failed):**
- A fault reaches production and causes degradation not detected by any signal within the window
- Triage engine attributes the fault to the wrong cause in the `EvidenceBundle` suspects list
- Rollback fails or leaves the platform in a degraded state

### Fault Catalog

A `docs/fault-catalog.md` documents every fault type used in adversarial testing:
- Fault ID, category (code / infra / load), expected detection method, expected recovery path
- Historical results: which faults the platform caught, which it missed, and what was improved

The catalog is updated after every adversarial test session. It becomes the living record of the platform's known failure modes and detection coverage.

### Deliverables

- `agents/` — agent configuration and scenario definitions (developer, devops, designer roles)
- `agents/fault-catalog/` — fault definitions with expected detection and recovery paths
- `agents/scenarios/` — scenario session configs (which agents, which faults, which services)
- `scripts/run-adversarial.py` — orchestrates an adversarial session: activates agents, monitors platform, collects session report
- `docs/fault-catalog.md` — living record of all fault types, detection results, platform improvements
- `docs/adversarial-testing-design.md` — design rationale, agent role definitions, pass/fail criteria, known limitations

---

## Milestones

- [x] Stage 1: Coverage thresholds enforced, schema contract tests in CI, Hypothesis suites for all external data parsers
- [ ] Stage 2: Collector gateway + bearer auth live with `weather` as the first collector, failure-path metrics emitting, Chaos Mesh running, every chaos scenario documented and passing against a criterion it is capable of failing, k6 harness wired with stub-mode reference baselines (regression gate deferred until real documents flow)
- [ ] Stage 3: Agent scaffolding built, first adversarial session run against weather and player-projections, fault catalog seeded with results

---

## Deliverables Summary

| Stage | Key Artifacts |
|---|---|
| Rigorous Testing | Coverage enforcement, schema contract tests, Hypothesis property tests, integration test suite |
| Chaos + Scale | Chaos Mesh installation, scenario manifests, k6 load scripts, chaos CI job |
| AI Adversarial Layer | Agent configs, fault catalog, scenario runner, session report format |

---

## Design Decisions

**Why three escalating stages, not one combined phase.**
Each stage validates a different threat model: Stage 1 catches developer mistakes before they ship. Stage 2 catches infrastructure failures that CI cannot simulate. Stage 3 catches the class of failures that only emerges when the full system — CI, deploy, observability, triage, rollback — is exercised end-to-end under realistic adversarial pressure. Combining them would make failures harder to attribute and the scope too large to execute incrementally.

**Why AI agents as adversaries, not purely random chaos.**
Random pod kills and resource starvation (Stage 2) test infrastructure resilience. But many real-world production incidents are caused by plausible, well-intentioned changes that happen to interact badly with the system. AI agents generate changes that look like real engineer output — they pass review heuristics, they compile, they often pass unit tests. This is the failure class that most testing frameworks do not cover.

**Why the designer agent is continuous, not scenario-based.**
Real product usage is not synchronized with infrastructure events. The designer agent applies background load throughout the session, making it harder for the detection layer to attribute degradation purely to a single deploy. This is closer to how production incidents actually present.

**Why the Phase 4 detection engine is in the critical path for Stage 3.**
Stage 3 is the first test where the triage engine must perform under adversarial conditions it was not explicitly designed for. The `EvidenceBundle` suspect ranking is part of the pass/fail criterion. This validates Phase 4 in a way that manual testing cannot — the detection engine must correctly rank suspects for faults it has not seen before, using only the signals the platform surfaces; the 4B narrator must then explain them clearly to the on-call engineer.

---

## Definition of Done

- [ ] All stage deliverables implemented and merged to `main`
- [ ] Tests green in CI; integration gate passing
- [ ] This doc's Status banner flipped to ✅ **Done** with the delivering PR
- [ ] README Phases table updated (Status + Landed)
- [ ] Milestone commit tagged `phase-5` and pushed — see [tagging-policy.md](../tagging-policy.md)

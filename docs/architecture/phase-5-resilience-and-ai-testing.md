# Phase 5 — Resilience Testing & AI Agent Adversarial Layer

**Goal:** Prove the platform survives real-world conditions — not just happy-path deployments. Introduce rigorous service and platform-level testing in three escalating stages: exhaustive automated testing, chaos and scale validation, and finally an AI agent adversarial layer where synthetic engineers are released against the platform to test whether it can detect, recover, and iterate under realistic failure modes.

---

## Overview

Phases 1–4 prove the platform works. Phase 5 proves it holds under pressure.

The three stages build on each other:

1. **Rigorous Testing** — close the gap between "tests exist" and "tests cover what matters." Coverage thresholds, contract testing, property-based testing, and load baselines.
2. **Chaos + Scale Testing** — inject infrastructure-level failures and drive the platform beyond its designed load envelope. Find the breaking points before production does.
3. **AI Agent Adversarial Layer** — release AI agents acting as realistic team members (developer, devops engineer, designer) that make plausible but intentionally flawed contributions. The platform must detect degradation, surface it to the incident assistant, and support rollback and iteration — all without human intervention in the detection loop.

---

## Stage 1 — Rigorous Service and Platform Testing

### What Gets Built

**Coverage enforcement.** `pyproject.toml` gains `[tool.pytest.ini_options]` with `--cov`, `--cov-fail-under=80`, and `--cov-branch`. PRs that drop coverage below threshold fail CI. Per-service coverage reports published as PR comments.

**Contract testing (Pact).** `player-projections` consumes `player-data` via S3 polling. A Pact consumer test documents the expected shape of the S3 payload. When `player-data` is built, it runs provider verification against the published contract. Contract mismatch blocks the consumer PR — the API contract is enforced by CI, not convention.

**Property-based testing (Hypothesis).** Services that parse external data (S3 projection payloads, weather API responses) gain Hypothesis tests that generate structurally valid but adversarial inputs: missing fields, wrong types, empty arrays, extremely large payloads. These tests are added to the standard `pytest` suite and run on every PR.

**Service integration tests.** A dedicated `tests/integration/` suite spins up each service with a real HTTP client (no mocks) and exercises edge cases: concurrent requests, upstream timeouts, malformed responses. These run in the integration test job alongside the Kind cluster smoke tests.

### Deliverables

- Updated `pyproject.toml` per service with coverage thresholds and branch coverage
- `tests/contract/` — Pact consumer tests for `player-projections` → `player-data`
- `tests/property/` — Hypothesis suites for all external data parsers
- `tests/integration/` — real HTTP integration tests per service
- `.github/actions/coverage-report/` — composite action: publishes per-PR coverage delta comment
- `docs/testing-strategy.md` — what is tested at each layer and why

---

## Stage 2 — Chaos Engineering and Scale Testing

### What Gets Built

**Chaos engineering with Chaos Mesh.** Chaos Mesh is deployed to the Kind cluster. A `chaos/` directory contains scenario manifests:
- `pod-kill.yaml` — kills a random service pod, validates auto-recovery within N seconds
- `network-partition.yaml` — blocks outbound calls from `player-projections`, validates stub-mode fallback activates
- `resource-pressure.yaml` — injects CPU and memory pressure, validates resource limits hold
- `latency-injection.yaml` — adds 2s of latency to `weather` upstream calls, validates timeout handling
- `bad-deploy.yaml` — deploys an image that crashes at startup, validates Argo CD health check fails the rollout and the previous version stays live

Each scenario has a defined **steady state** (what "healthy" looks like before), a **hypothesis** (what the platform should do under the failure), and a **pass/fail criterion** checked via Prometheus queries after the scenario runs.

**Load and scale testing with k6.** `tests/load/` contains k6 scripts for each service:
- Ramp test: 0 → 100 RPS over 5 minutes, measure P95 latency and error rate
- Soak test: 50 RPS sustained for 30 minutes, detect memory leaks or connection pool exhaustion
- Spike test: 10x normal load for 60 seconds, validate graceful degradation
- Breakpoint test: ramp until error rate exceeds 1%, document the service's failure threshold

Load test results are published as artifacts and compared against a documented baseline. Regressions (P95 latency increasing >20% vs. baseline) fail CI.

**Chaos + scale CI job.** A new workflow `chaos-test.yml` runs on the `ready-for-chaos` label (separate from `ready-for-merge`). Spins up the Kind cluster, runs the full chaos suite, then runs load tests. Results published to PR as a structured report: scenario name, hypothesis, observed behavior, pass/fail.

### Deliverables

- `infra/chaos-mesh/` — Chaos Mesh helmfile installation
- `chaos/scenarios/` — Chaos Mesh scenario manifests with documented hypotheses
- `tests/load/` — k6 scripts per service
- `.github/workflows/chaos-test.yml` — label-triggered chaos + load test job
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
- Repeated calls to stub endpoints that will be populated by `player-data`
- Requests for new endpoints that do not exist yet (404 rate increase)

The designer agent's pressure is continuous throughout the adversarial test run, not isolated to individual scenarios.

### Test Execution Model

Each adversarial test run is a **scenario session**:

1. **Setup** — cluster running, all services healthy, baselines recorded
2. **Agent activation** — one or more agents are given tasks and begin making changes
3. **Platform observation** — changes flow through CI, deploy via GitOps, observability captures state
4. **Degradation window** — if a fault reaches production, the observation layer detects it
5. **Triage** — `foundry triage` is invoked automatically when error rate exceeds threshold
6. **Recovery** — rollback or fix is applied; platform returns to steady state
7. **Session report** — structured output: what changed, what broke, what the incident assistant surfaced, how long recovery took, what the platform missed

**Pass criteria:**
- Faults that fail CI (lint, test, helm-lint) must be caught before merge
- Faults that pass CI but degrade production must be detected within N minutes
- Incident assistant must correctly identify the deploy or config change as the likely cause
- Rollback must restore steady state within N minutes
- Platform must reach healthy state before the next scenario begins

**Failure criteria (platform failed):**
- A fault reaches production and causes degradation not detected by any signal within the window
- Incident assistant attributes the fault to the wrong cause
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

- [ ] Stage 1: Coverage thresholds enforced, contract tests in CI, Hypothesis suites for all external data parsers
- [ ] Stage 2: Chaos Mesh running, all 5 chaos scenarios documented and passing, k6 baselines established for all services
- [ ] Stage 3: Agent scaffolding built, first adversarial session run against weather and player-projections, fault catalog seeded with results

---

## Deliverables Summary

| Stage | Key Artifacts |
|---|---|
| Rigorous Testing | Coverage enforcement, Pact contract tests, Hypothesis property tests, integration test suite |
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

**Why the incident assistant is in the critical path for Stage 3.**
Stage 3 is the first test where the incident assistant must perform under adversarial conditions it was not explicitly designed for. Its output is part of the pass/fail criterion. This validates Phase 4 in a way that manual testing cannot — the assistant must reason about faults it has not seen before, using only the signals the platform surfaces.

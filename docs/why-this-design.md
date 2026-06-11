# Why This Design

This document explains the key architectural decisions in Foundry — not what was built, but why. These are the tradeoffs that matter for operational correctness and platform scalability.

---

## Why Helm

Helm is the most widely understood Kubernetes packaging format. It has broad toolchain support (CI validators, security scanners, artifact registries) and is the de facto standard in the organizations this platform targets.

The alternative — raw Kustomize — offers more composability at the cost of discoverability. For a platform designed to be understood by engineers onboarding new services, Helm's explicit `values.yaml` contract is clearer than Kustomize overlays.

**The tradeoff accepted:** Helm templates are verbose and occasionally awkward. This is acceptable because the chart complexity is bounded — services use a standard chart, not bespoke per-service charts.

---

## Why GitOps

GitOps (via Argo CD) makes the cluster state auditable and reproducible from a single source of truth. Every deploy is a git commit, every rollback is a git revert. The cluster cannot drift from what is declared in the repo.

The alternative — CI-driven `helm upgrade` on every push — is simpler to set up but loses auditability and makes rollback manual and error-prone.

**The tradeoff accepted:** GitOps adds a reconciliation delay (seconds to a few minutes) between a commit and a live deploy. For the services in this platform, this is acceptable. High-frequency deploys requiring sub-second convergence would need a different model.

---

## Why OpenTelemetry

OTel is the vendor-neutral standard for instrumentation. Instrumenting once with OTel means the telemetry backend can change without touching service code. This is the right default for a platform that controls the observability stack but not the services' business logic.

The alternative — direct SDK integration with a vendor (Datadog, New Relic) — creates lock-in at the service layer. Once a service imports a vendor SDK, switching backends requires code changes across every service.

**The tradeoff accepted:** OTel SDKs are more complex to configure than vendor agents. This complexity lives in the platform layer (`foundry_telemetry` module), not in individual services. Services get observability by importing one module.

---

## Why Shared Observability Backend (not sidecars)

All services emit telemetry to a single OTel Collector instance (DaemonSet-style), which fans out to Loki, Tempo, and Prometheus. Services do not run their own collector sidecars.

The sidecar model offers better isolation — one service's telemetry pipeline cannot affect another's. But it multiplies resource cost and operational complexity linearly with service count.

The shared collector model is correct for this scale. The collector is a platform component with its own reliability posture. Services are just OTLP clients.

**The tradeoff accepted:** A failing collector affects all services' telemetry simultaneously. Mitigation: the collector is kept simple (no heavy processing), and services buffer in-process before dropping.

---

## Why Grafana LGTM Stack (Loki + Grafana + Tempo + Mimir/Prometheus)

The LGTM stack is open source, self-hostable, and integrates natively with the OTel ecosystem. Running a unified stack locally means the local dev experience matches production observability — engineers can query real traces and logs without deploying to a shared environment.

The alternative — using managed cloud observability (Datadog, Honeycomb) — would require cloud accounts and internet access for local dev, and would obscure the platform layer that Foundry is designed to demonstrate.

**The tradeoff accepted:** Self-hosting Loki, Tempo, and Prometheus adds operational surface area. For a local Kind cluster, this is fine. For a production platform, managed backends (Grafana Cloud, etc.) would be the right default.

---

## Why the Incident Assistant is Assistive Only

The triage assistant gathers context and reasons about likely causes. It does not take action.

Autonomous remediation (auto-rollback, auto-scaling) requires a level of system understanding and failure-mode coverage that is hard to verify. A bad auto-rollback during a database migration, for example, can cause more damage than the original incident.

The right boundary for AI in incident response is: surface context faster, reduce cognitive load during high-stress situations, suggest next checks. The engineer retains full agency over what happens next.

**The tradeoff accepted:** Faster time-to-resolution from automation is left on the table. This is a deliberate, correct tradeoff for this system's scope and maturity level.

---

## Why Merge-Queue Integration Tests (Not Per-Push)

Running a full Kind cluster + stack deploy on every PR push would add 5-10 minutes to every iteration cycle. Developers push frequently while working — running the cluster gate that often is wasteful and creates false urgency around fixing flaky infra tests mid-feature.

The GitHub merge queue is a deliberate signal: "I'm done, this is ready to ship." When a developer marks a PR "Merge when ready," GitHub adds it to the queue, rebuilds it on top of the latest `main`, and runs the integration test against that combined ref — merging only if it passes. PRs iterate fast without triggering the cluster; the gate fires exactly once, at merge time. Changes that do not touch the deployable surface (`services/`, `helm/`, `infra/`, `scripts/`) auto-skip the test and merge immediately.

**The tradeoff accepted:** The integration test runs in the merge queue, not on the PR branch itself. A developer cannot pre-validate the cluster behavior before queuing. This is acceptable because the test validates the merged state (PR + latest main), which is the correct artifact to gate on.

---

## Why GITHUB_TOKEN (Not a PAT) for GitOps Commits

The `update-gitops-tag` action commits to `infra/gitops/` using `GITHUB_TOKEN` with `contents: write` permission. A PAT would also work but requires storing a long-lived credential as a repo secret, rotating it manually, and binding it to a specific user account.

`GITHUB_TOKEN` is scoped to the workflow run, expires automatically, and requires no secret management. It's sufficient here because Argo CD watches the repo directly — the gitops commit doesn't need to trigger another GitHub Actions workflow.

**The tradeoff accepted:** A commit from `GITHUB_TOKEN` cannot trigger other workflows (GitHub prevents this to avoid loops). This is fine — the gitops commit is a terminal step, not a trigger for further CI.

---

## Why App-of-Apps (Not Individual kubectl apply)

The app-of-apps pattern means every Argo CD Application is itself managed by Argo CD. Adding a new service = adding a YAML file to `infra/gitops/argo/` and pushing. No manual `kubectl apply`, no Argo CD UI clicking.

The alternative — creating Applications manually — works for one or two services but breaks the GitOps principle: if the Application definition isn't in git, it can drift, get deleted, or be impossible to recreate from scratch.

**The tradeoff accepted:** The app-of-apps adds one layer of indirection. When debugging, you need to understand both the parent app (which manages child Applications) and the child apps (which manage services). This is worth it for any number of services beyond one.

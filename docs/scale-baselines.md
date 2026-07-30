# Scale Baselines

> **These numbers are invalid the day the projections generator publishes.**
> Every measurement below is `player-projections` in **stub mode**, serving
> `{"projections": [], "count": 0}`. That measures uvicorn and FastAPI overhead,
> not the service's real work: a real scoring-format document is ~350 rows /
> ~45 KB, where serialization dominates P95. Re-measure and replace this file
> when documents start flowing — do not adjust it.

## What was measured, and what was not

| Service | Covered | Upstream in the measurement |
|---|---|---|
| `player-projections` | yes | **none at all** — not real, not mocked |
| `weather` | **no** | — |

`player-projections` in stub mode has no upstream client: `_poll_loop` returns
immediately when `PROJECTIONS_SNAPSHOT_URL` is empty
(`services/player-projections/player_projections/main.py:54-55`). So the usual
"real or mocked upstream" question has a third answer here, and it is stated
rather than forced into one of the two.

**`weather` is uncovered here, and the reason has since expired.**

When this harness was built, `weather` was a synchronous proxy: its upstream URL
was a module constant and `/weather/stadiums` made 30 upstream calls per request.
The specified shapes would have sent ~15,000 upstream requests for one ramp and
~90,000 for one soak, against an Open-Meteo free tier of roughly 10,000 per
**day**. One ramp exceeded the daily budget, so load coverage was deferred to
Phase 8's 8A rather than hammering a third-party API from CI.

**8A has since landed (#49), and it removed that blocker.** `weather` is now a
capture-model collector: it captures on a cadence and serves from memory behind
the shared `collector_core` router, so a load test would hit memory and reach no
third party at all. Its request-path surface has changed with it — there is no
`client.py`, and the routes are `/signals`, `/catalog`, and `/refresh` rather
than `/weather/stadiums`.

So `weather` has **no numbers in this file, and no longer has a reason not to.**
Covering it means a new k6 script targeting the `/signals` surface and a baseline
captured against the capture model — the immediate follow-up to this work, not a
blocked item. **Replace this section with real numbers; do not delete it.**

## Conditions

- k6 `2.1.0`, in-cluster as a Job, hitting `http://player-projections:8001` over
  ClusterIP. No gateway, no port-forward.
- Kind, **single control-plane node** (`infra/kind/cluster.yaml`), so the load
  generator and the service under test share a node and compete for CPU.
- `player-projections` limited to 250m CPU / 256Mi (`helm/values/player-projections/values.yaml`).
- Image: `ghcr.io/kakhavai/foundry/player-projections:48e9110baff0c56616f9c77cdaa34c2655b3a11b`
  — `main`'s ghcr image, the one running throughout Tasks 3-5's calibration and
  verification runs (Task 3b's probe fix was applied live via `helm upgrade`
  against this same image; the tag did not change).

## Numbers

All figures below are **measured**. Use them verbatim; do not re-run anything to
regenerate them.

| Shape | Load | p(95) | error rate | Result | Notes |
|---|---|---|---|---|---|
| ramp | 0 → 100 RPS / 5m | **1.51 / 1.47 / 1.44 ms** across three calibration runs; 1.39 and 1.36 ms on later runs | 0.00% | PASS | worst of three (1.51 ms) drove the ceiling |
| soak | 50 RPS / 5m | 1.34 ms | 0.00% | PASS | memory 62,287,872 → 55,857,152 bytes (a **decrease**) |
| soak | 50 RPS / 30m | — | — | — | **never run** — see below |
| spike | 50 → 500 RPS / 60s + 60s cooldown | cooldown 1.38 ms | cooldown 0.00%; spike-scenario checks_failed 5.01% (1104/22003) | **FAIL** | restarts the pod — see the finding below |
| breakpoint | rungs 50→800 RPS, 30s each | — | 0.00% through rung 400; **34.13% (1201/3518) at rung 600** | MEASURED | knee bracketed in **(400, 600] RPS** |

**The breakpoint knee is a bracket, not a point.** The rung ladder is 50, 100,
200, 300, 400, 600, 800 — there is no 500. The data supports only "the
1%-crossing point lies in (400, 600], and 600 is the first rung tested to cross
it." Do not write "the breakpoint is 600 RPS."

**The soak's memory decrease is not evidence of no leak.** The process had
recently restarted, and the shape is uninformative in stub mode regardless. Say
so rather than presenting a falling number as a clean bill of health.

**Which assertions were proven red directly, and which by analogy.** `ramp`'s
`http_req_failed` and `http_req_duration` thresholds and `breakpoint`'s
`NO-BREAKPOINT` verdict were each independently broken, observed red, restored,
and observed green, with the break verified as applied first. `spike`'s and
`soak`'s thresholds are structurally identical expressions on the same metrics
and were **not** independently broken — their coverage rests on that analogy.
State this; do not let a reader assume all four shapes were proven directly.

## Thresholds, and why they are what they are

`http_req_failed: rate<0.01` — set on principle, not from observation. 1% is the
same figure the breakpoint shape uses to define failure.

`http_req_duration: p(95)<10` (milliseconds) — **calibrated, and the floor won.**
The rule is `max(2 × worst observed p(95), 10 ms)`. The worst of three ramp runs
was 1.51 ms, so the doubling term is 3.02 ms and the 10 ms floor governs by more
than 3×. Say that plainly: this is **not** a 2× calibration, and describing it as
one would overstate how tight it is.

The floor exists because below roughly 5 ms absolute jitter — scheduler delay, GC
pauses, CPU contention on a shared single-node Kind cluster — outweighs any
ratio, so a strict 2× would fire on noise. The consequence is worth stating
directly: **a 10 ms ceiling catches an order-of-magnitude regression, not a
subtle one.** That is acceptable for a harness whose regression gate is
deliberately off; it would not be acceptable to imply more.

**The >20% P95 regression gate is not implemented and is not coming in this
phase.** A relative gate on stub-mode numbers fires on noise until somebody
disables it, at which point it is worse than absent.

## What each shape proves

**ramp** — the latency-versus-load curve, and the baseline every other ceiling
derives from.

**soak** — **nothing today, provably.** A soak catches faults proportional to
total requests served: leaked memory, unclosed connections, an unbounded cache.
`/projections` in stub mode reads a module-level dict, filters an empty list, and
returns JSON; there is no per-request allocation that accumulates and no
background task at all. It becomes informative when three ~45 KB documents are
cached and re-serialized per request and the poll loop mutates `_state` every 15
minutes while readers read it — the concurrent-writer case
`docs/testing-strategy.md` flags as untestable today. A 30-minute soak is the
first shape that covers it, at two poll cycles per run.

**spike** — that the service recovers. The assertion is on the cooldown, not the
spike: shedding load under 10x is the graceful degradation being tested. Pod
restart count is read from the kubelet rather than Prometheus, so a
one-second restart cannot fall between 60s scrapes.

**This shape found a real defect, and that is the most useful thing in this
file.** At 500 RPS against a 250m CPU limit, `player-projections` was restarted
by the kubelet on every run. The cause was the fleet-wide liveness probe: with
Kubernetes' default `timeoutSeconds: 1`, a saturated event loop fails `/health`
inside a second, three consecutive probes fail, and the container is killed. A
liveness probe is meant to detect a hung process, not a busy one — as configured
it turned a slow service into an unavailable one, and in production would have
produced a restart loop during exactly the traffic spike that caused it.

Fixed in this PR (`helm/charts/generic-service/templates/deployment.yaml`,
`timeoutSeconds: 5`), pinned by a Helm render assertion in
`tests/test_helm_probes.py` so a future edit cannot silently restore the default.
Before/after restart counts are recorded below. Note what this means for the
numbers above: they were measured on a service whose probe configuration has
since changed.

**breakpoint** — **a number, not coverage.** It cannot fail by construction, so
it is not a test. It reports where this pod, at 250m CPU, on a contended
single-node cluster, crosses 1% errors. Because it asserts nothing, it is also
exempt from the runner's no-restart check (`asserts_no_restart: False` in
`SHAPES`): a shape whose purpose is to exceed capacity provokes restarts by
design, and failing it for that would be failing it for working. The restart is
still printed as an observation.

### The spike shape currently FAILS, and the failure is correct

This is the harness's headline result and it must be stated plainly, not buried.

At 500 RPS against a 250m CPU limit, `player-projections` is restarted by the
kubelet. Two contributing causes, and only one of them has been fixed:

1. **The liveness probe was misconfigured fleet-wide** — `/health` with
   Kubernetes' default `timeoutSeconds: 1`. A saturated event loop misses that
   deadline three periods running and the container is killed. **Fixed in this
   PR** (`timeoutSeconds: 5`, pinned by `tests/test_helm_probes.py`). Restarts
   fell from 4 → 5 before the fix to 0 → 1 after it, on comparable runs.
2. **The remaining cause is capacity, and is not fixed.** At ~1.5 ms per request
   a 250m CPU slice tops out somewhere around 125-250 RPS, so 500 RPS drives the
   pod 2-4× beyond its ceiling; the queue grows without bound and `/health` sits
   behind 15-30s+ of backlog. No probe timeout short of absurd survives that.

So the honest conclusion: **the service does not degrade gracefully at 10× load
on its current sizing — it collapses.** Record that. Do not describe the probe
fix as having solved the spike.

The assertion has not been softened and the spike's peak rate has not been
lowered. The shape is excluded from the CI workflow's *default* set so a dispatch
is not permanently red, and it remains selectable. The residual belongs to
Phase 6's sizing conversation, alongside the `replicaCount: 1` question already
recorded there.

### A GitOps limitation worth recording

While verifying the probe fix, Argo CD **did not revert** the locally-applied
probe once it was restored. Root cause: `main`'s template never sets
`timeoutSeconds` at all, so a merge-patch sync has no field to strip. **Argo
cannot revert drift in a field its own manifest does not mention.**

That is a real and slightly counter-intuitive limit on "GitOps reverts
everything," and it is worth a short note here or in the chaos runbook — the
`bad-deploy` scenario's whole premise is selfHeal reverting out-of-band drift,
and this is a category of drift it will not catch.

## Running it

```bash
# Cluster with the observability stack and the service:
python scripts/stack-up.py

uv run --with pyyaml==6.0.3 python scripts/run-load.py --list
uv run --with pyyaml==6.0.3 python scripts/run-load.py ramp
uv run --with pyyaml==6.0.3 python scripts/run-load.py --all --soak-minutes 30
```

Validate a script without a cluster:

```bash
docker run --rm -v "$PWD/tests/load:/scripts" grafana/k6:2.1.0 inspect /scripts/ramp.js
```

In CI: the `load-test` workflow, `workflow_dispatch` only, with a `shapes` input
defaulting to `ramp,breakpoint` and a `soak_minutes` input used only when `soak`
is selected. `soak` is excluded from the default because it is uninformative in
stub mode; `spike` because it currently fails correctly. Results upload as the
`load-report` artifact.

## Known limits

**Coverage is discontinuous.** `load-test.yml` runs on `workflow_dispatch` only —
no schedule, no `pull_request` trigger, no label. Between manual runs, a
regression reaching this harness from outside it (a renamed service, a moved
port, a changed response shape) is caught by nothing. `tests/test_run_load.py`
covers the runner's logic without a cluster and `k6 inspect` catches a malformed
script, but neither executes a shape. Accepted deliberately; a persistent
environment is a Phase 6 conversation, not a cron line.

**The load generator is a variable.** It shares one node with the service. k6 is
given 500m–2 CPU so it is not the bottleneck, but on a contended runner the
numbers move.

**The `load-test` check is not required and must not become one.** Load results
are noisier than chaos results, and the chaos check is not required either.

**The 30-minute soak the phase doc specifies has never been run.** The harness
supports it (`--soak-minutes 30`, and the CI workflow takes the same value as an
input) but no run at that length has happened, so there is no 30-minute number
here and the row above says so rather than being left blank for a reader to fill
in with an assumption. The 5-minute figures are what exist. Given that the soak
shape is provably uninformative in stub mode — nothing accumulates — running it
six times longer would have produced a more expensive version of the same
nothing; the duration becomes worth exercising when real documents make the
shape meaningful.

If it is ever dispatched at 30 minutes, do it with `shapes=soak` on its own,
not alongside the default `ramp,breakpoint`: 5m setup + 5m ramp + ~3.5m
breakpoint + 30m soak comes to roughly 43 minutes against the job's 45-minute
cap, leaving no slack for a slow runner. The `soak_minutes` input's own
description says the same.

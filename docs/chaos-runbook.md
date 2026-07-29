# Chaos Runbook

How to run the chaos scenarios, how to read a result, and what is known to go
wrong.

## Prerequisites

A running Kind cluster with the observability stack, the gateway, the services,
and Chaos Mesh:

```bash
python scripts/stack-up.py                     # cluster, observability, gateway, services
cd infra/chaos-mesh && helmfile repos && helmfile apply && cd ../..
```

`bad-deploy` additionally needs Argo CD:

```bash
python scripts/argocd-deploy.py install --env local
```

Chaos Mesh is deliberately **not** installed by `stack-up.py` — it is cost with
no benefit on a stack you are not breaking.

## Running a scenario

```bash
uv run --with pyyaml==6.0.3 python scripts/run-chaos.py --list
uv run --with pyyaml==6.0.3 python scripts/run-chaos.py pod-kill
uv run --with pyyaml==6.0.3 python scripts/run-chaos.py --all
```

`uv run --with pyyaml` rather than plain `python`: the CI runner image has
`python3` and `uv` but no PyYAML, and this keeps the local and CI invocations
identical.

Exit code is non-zero if any criterion failed.

## Reading a result

```
steady state:
  [PASS] weather is up and being scraped: 1 (want == 1)

injecting fault
holding for 30s
removing fault
settling for 90s

criteria:
  [PASS] a replacement pod was actually created: 2 (want >= 2)
  [PASS] weather is being scraped again: 1 (want == 1)

result: PASS
```

Three outcomes are worth telling apart:

- **`ABORT: the system is not in its steady state`** — nothing was injected. The
  cluster was already unhealthy, so any result would have been unattributable.
  Fix the cluster, then rerun.
- **`no data (set allowEmpty if this is expected)`** — the query returned an
  empty result. Prometheus answers identically for a series that has never
  existed and for a typo'd metric name, so the runner refuses to guess. If
  absence is genuinely correct, set `allowEmpty: true` on that check.
- **`FAIL`** — the hypothesis did not hold. This is the interesting one.

## Scenarios

| Scenario | Injects | Asserts |
|---|---|---|
| `pod-kill` | Kills weather's only pod | A replacement is created and scraping resumes unaided |
| `resource-pressure` | 400MB memory hog against a 256Mi limit | The cgroup OOM-kills the container rather than eating node memory; player-projections is untouched and weather restarts to Ready |
| `latency-injection` | 12s egress delay to Open-Meteo | Captures time out and the counter says so |
| `network-partition` | Partitions the Envoy data plane from weather | The gateway loses its upstream; weather stays healthy |
| `bad-deploy` | Sets a nonexistent image out of band | Argo's selfHeal reverts the drift |

Each scenario declares a steady state, a hypothesis, and criteria in a
`foundry.chaos/v1` head at the top of its file, followed by the Chaos Mesh
resources that inject the fault.

## Known failure modes

**Chaos coverage decays silently between sessions.** `chaos-test.yml` is
`workflow_dispatch` only — no label, no nightly schedule. Chaos coverage
therefore exists only when somebody runs it. This is accepted, not overlooked:
a schedule spends a Kind cluster every night on a repo that changes in bursts,
and a label needs a human to remember it. The consequence is real, so it is
written down here rather than discovered later.

**A chaos run tests `main`'s images, not your branch's.** `argocd-deploy.py
install` applies app-of-apps, and the Applications pin `targetRevision: main`
against GitHub. Argo therefore replaces whatever `deploy-local.py` built with
`main`'s images. For a `workflow_dispatch` run this is arguably more realistic —
it is the actual GitOps deployment path — but do not read a chaos result as a
verdict on uncommitted code.

**Argo will fight scenarios that mutate managed state.** `selfHeal: true` and
`prune: true` mean a manual `kubectl patch` or `apply` on a managed resource is
reverted within seconds, including on the Application objects themselves. That
is the point of `bad-deploy`, but it will also silently undo debugging changes
you make by hand.

**`kubectl wait --for=condition=ready pod -l <label>` is a trap here in
particular.** The label selector also matches pods left `Terminating`, which
never reach Ready, so it times out on a Deployment that is perfectly healthy.
Chaos tooling kills pods constantly, so `Terminating` pods are the normal case.
Use `kubectl rollout status deployment/<name>`.

**`/weather/stadiums` can take ~300s under total upstream failure.** It loops 30
stadiums sequentially at a 10s timeout each with no overall request deadline.
This is why the traffic driver calls `/weather/stadiums/{id}` instead — a single
list request would outlive the scenario. The missing deadline is a real latent
defect, surfaced by designing `latency-injection`; it belongs to whichever phase
rebuilds `weather` onto the capture model (Phase 8's 8A).

**The gateway has two blast radii, and only one drops traffic.** Killing the
**control plane** (`deployment/envoy-gateway`) does *not* interrupt requests —
Envoy keeps serving its last pushed configuration; what breaks is
reconfiguration, so a new `HTTPRoute` silently fails to take effect. Killing the
**data plane** (selected by `gateway.envoyproxy.io/owning-gateway-name=foundry`,
never by name — it carries a generated hash) *does* drop traffic through
`localhost:8080` while `svc/weather` stays reachable in-cluster. A scenario that
kills the control plane and asserts traffic stops would be wrong.

**A scenario can refuse to start right after a previous run, and that is a
feature.** `latency-injection` and `network-partition` both assert in their
steady state that the metric their criterion measures has been quiet recently,
because both criteria look back five minutes. Run either twice in quick
succession and the second run aborts on its own predecessor's evidence.

This guard is not paranoia. Without it on `network-partition`, a deliberately
neutered run — one whose fault selector matched nothing at all — scored **6.15**
and PASSED, purely on residue from the run before it. A green result proving
nothing is the exact failure this directory exists to prevent, and a lookback
window is an easy way to manufacture one.

Both guards use a [2m] window so a re-run is delayed by about two minutes rather
than blocked for five. An abort here is the runner working correctly. Wait and
retry; do not remove the guard.

**Every query must reduce to one series.** Service metrics are scraped twice —
`job="weather"` (static, via the Service) and `job="kubernetes-pods"`
(annotation, per-pod) — with identical values. A query without a `job` filter or
an aggregation resolves to two series, and the runner raises
`query returned 2 series; a check must reduce to one` instead of returning a
verdict. If a scenario crashes rather than failing, check this first.

**Metrics only appear when OTel is configured.** `collector_capture_*` and
`collector_auth_failures_total` use the OpenTelemetry meter, so they are present
on a deployed pod (the Helm chart injects `OTEL_EXPORTER_OTLP_ENDPOINT`) and
absent when a service is run locally. An empty result locally is not a broken
metric.

## Adding a scenario

1. Create `chaos/scenarios/<name>.yaml` with a `foundry.chaos/v1` head followed
   by the Chaos Mesh resources.
2. Give it a steady state, a hypothesis, and at least one criterion **that some
   observation could falsify**. `tests/test_chaos_scenarios.py` rejects
   criteria such as `>= 0` that no observation can falsify, but it cannot catch
   a criterion that is merely unlikely to fail.
3. Prove it can go red: break what it guards, capture the red output, restore,
   capture green. Verify the break actually applied before trusting the red —
   a revert script with a syntax error once "proved" red against unmodified
   code.
4. Run `uv run --with pyyaml==6.0.3 --with pytest==9.0.3 pytest tests/test_chaos_scenarios.py -q`.

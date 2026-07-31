# Writing a Collector

The guide for the person adding collector number five through twenty-six. It
covers what the shared library expects of you and the handful of things that
fail *silently* if you get them wrong. It deliberately does not restate the
architecture — [`CLAUDE.md`](../CLAUDE.md) owns the gateway, the bearer-token
decision, GitOps behaviour and the registry's rules, and
[`docs/architecture/phase-8-data-source-collectors.md`](architecture/phase-8-data-source-collectors.md)
owns the fleet plan.

---

## Start with the scaffolder. Do not copy a collector.

```bash
uv run --no-project --with pyyaml==6.0.3 python3 scripts/new-collector.py \
    betting-lines --cadence volatile --signal-types game_line_snapshot
uv lock
cd services/betting-lines && uv run pytest -v
```

`--cadence` is a [`CadenceClass`](../libs/collector-core/collector_core/cadence.py)
value. `--signal-types` is comma-separated snake_case. The port is assigned one
past the highest already claimed unless you pass `--port`. Add `--depends-on`
for collectors yours needs (they must already be registered), `--scope-aware` if
you narrow to `roster-scope`'s membership list, and `--smoke-hook` only if you
have routes beyond the standard five.

Copying an existing collector instead is how the fleet re-acquires the files
Wave 0 deleted. The scaffolder generates **exactly**:

```
services/<name>/<pkg>/__init__.py
services/<name>/<pkg>/main.py                 the descriptor, and nothing else
services/<name>/<pkg>/capture.py              the capture pass
services/<name>/<pkg>/metrics.py              your own series, on a subclass
services/<name>/<pkg>/signals.py              SUPPORTED_FILTERS + signal_matches
services/<name>/<pkg>/adapters/upstream.py    the only module that knows the wire
services/<name>/Dockerfile
services/<name>/pyproject.toml
services/<name>/README.md
services/<name>/tests/{conftest,test_routes,
                      test_capture_contract_conformance,
                      test_coverage_floor}.py
services/<name>/smoke.sh                      optional, --smoke-hook only
helm/values/<name>/values.yaml
infra/gitops/envs/local/<name>/values.yaml
contracts/signal-envelope/collectors/<name>.json
contracts/collector-registry.yaml             one appended entry
```

and deliberately **not**: a CI workflow (`services.yml` is a matrix computed
from the fleet), an Argo CD Application (`applicationset.yaml` globs
`helm/values/*`), a `telemetry.py`, an `auth.py`, a `scheduler.py`, per-member
Dockerfile `COPY` lines, or an edit to `deploy-local.py`, `stack-up.py`,
`smoke-test.sh` or `integration-test.yml`.

**If you find yourself editing one of those, stop.** That is a gap in the
tooling, not a step you are missing. `tests/test_collector_tooling.py` exists
to make it a test failure rather than a habit, and
`tests/test_new_collector.py` proves a freshly generated collector needs none
of them.

---

## The descriptor

Everything a collector process needs that is not the descriptor —
environment parsing, `CaptureState`, `RefreshGate`, the lake writer, the
lifespan, the capture loop, bearer auth, the OTel guard, and the five standard
routes — is `build_collector_app`'s job. See
[`collector_core/app.py`](../libs/collector-core/collector_core/app.py).

| Field | Required | What it is |
|---|---|---|
| `name` | yes | The collector name. Must equal the registry entry, the directory under `services/`, and the Helm `service.name` |
| `cadence_class` | yes | Sets the loop's base interval and how `collector_staleness_seconds` is alerted |
| `signal_types` | yes | One envelope per entry, every pass |
| `supported_filters` | yes | What `GET /signals` accepts. Anything else is 422, never silently ignored |
| `capture` | yes | `(season, week, *, client, lake, now, deadline=None) -> dict[str, Envelope]` |
| `signal_matches` | yes | `(row, params) -> bool`, for the filters beyond the universal three |
| `metrics` | yes | Your `CollectorMetrics` **instance** |
| `next_event_at` | no | Only if you have a perishable moment to escalate toward. `weather`'s `next_kickoff` is the fleet's only one |
| `telemetry_module` | no | **Leave it alone.** See below |
| `client_factory` | no | Only if your upstream needs a different transport than `httpx.AsyncClient(timeout=10.0)` |

### The two that fail silently

**`telemetry_module` is a dotted string, never a callable.** It defaults to
`"collector_core.telemetry"`, and `build_collector_app` imports it with
`importlib` *inside* the `OTEL_EXPORTER_OTLP_ENDPOINT` guard. Importing your
telemetry module at the top of `main.py` and passing the bound function instead
is legal Python, and every test stays green — but it pulls the OTel SDK, the
exporters and the instrumentors in eagerly, which is the exact thing the guard
exists to prevent. A string cannot import anything by itself; that is why the
field takes one. Do not write a `telemetry.py`: Wave 0 deleted three identical
copies of it.

**`metrics` is passed in, not constructed by the library.** Your `capture`
already imports its metrics instance, and there must be exactly one per
process — two instances means two sets of OTel instruments and half your
recordings landing on a series nobody queries. Build it once at module level in
`metrics.py` and hand that object to the descriptor.

### Your own metrics belong on a subclass

`collector_core.metrics.CollectorMetrics` owns the fleet-wide series
(`collector_capture_requests_total`, `collector_capture_failures_total`,
`collector_coverage_ratio`, `collector_staleness_seconds`,
`collector_auth_failures_total`). Series that answer *"is this collector wrong
in the way only it can be wrong"* go on a subclass in your own `metrics.py` —
see `roster-scope`'s `scope_missed_producers` and `player-identity`'s
`identity_merge_conflicts`. A metric only one service records must not grow
into the shared library.

Record on every pass, including zero. An absent Prometheus series and a healthy
one are indistinguishable in PromQL, so a gauge written only when it is
interesting cannot be alerted on.

**Build every gauge with
[`LastValueGauge`](../libs/collector-core/collector_core/metrics.py), never
`meter.create_gauge`.** Recording on every pass is only half the job: OTel's
*synchronous* gauge is last-value aggregated with the point **consumed** by a
collection, so it is exported on the first scrape after a recording and absent
from every scrape after that. Collectors record on a capture cadence — minutes
to hours — and Prometheus scrapes every 15-30 seconds, so the overwhelming
majority of scrapes saw nothing at all. That is the exact failure the
"record even at zero" rule exists to prevent, arriving by a different route,
and it is why `scripts/run-chaos.py` treating an empty result as a hard error
would have made any criterion on `collector_coverage_ratio` flaky by
construction.

`LastValueGauge` wraps `create_observable_gauge` behind the same
`.set(value, attributes)` call, so the only thing that changes is the
constructor:

```python
self._rows_captured = LastValueGauge(
    meter,
    "betting_lines_rows_captured",
    description="Rows captured in the last pass, by collector.",
)
...
self._rows_captured.set(count, {"collector": self.collector})
```

Two consequences. The callback runs at **collection** time on whichever thread
drives the scrape — the event loop, for `/metrics` — so it must never block and
must never reach the lake, which raises on a loop-thread call. And a label set,
once written, is reported forever; that is the point, and it means the wrapper
is only safe for **bounded** label sets. Every gauge in the fleet is keyed by
`collector` plus at most `signal_type`. Do not put a `player_id` in one.

Counters (`meter.create_counter`) are a different instrument class, already
cumulative, and need none of this.

**Test it with two consecutive scrapes.** A single scrape passes either way,
which is why this survived nine collectors. `libs/collector-core`'s
`test_coverage_ratio_is_present_on_a_second_consecutive_scrape` is the model,
and each service's `tests/test_metrics.py` pins its own series the same way.

---

## Narrowing to a scope: `ScopeClient` and `IdentityClient`

`--scope-aware` on the scaffolder records the flag on your registry entry
today; it does not yet generate the wiring below. That retrofit is deferred
work (this repo's scope-narrowing plan calls it out explicitly as future
work), but the two seams it will retrofit onto already exist in
`libs/collector-core` and are unit-tested on their own, independent of any
collector that calls them:
[`collector_core/scope.py`](../libs/collector-core/collector_core/scope.py)
and [`collector_core/identity.py`](../libs/collector-core/collector_core/identity.py).

**`ScopeClient` reads the published scope from the lake, never from
`roster-scope` over HTTP.** `roster-scope`'s `GET /scope/players` and
`GET /scope/matchups` exist for the out-of-repo generator and for operators —
a person or process asking "what does the scope look like right now."
A collector narrowing its own fetch asks a different question — "what was the
last scope this fleet agreed on" — and the lake, not a live HTTP call, is the
answer that survives a `roster-scope` outage. `roster-scope`'s own capture
writes its membership envelope there on every pass; `ScopeClient.fetch(...)`
reads the newest one back:

```python
scope = await ScopeClient(lake).fetch(MEMBERSHIP_SIGNAL, season, week)
# scope.members: frozenset[str] of player_id
# scope.captured_at: datetime — age it against `now` before trusting it
```

It **fails closed on two distinct empty states**, and both raise
`ScopeUnavailable` rather than returning something a caller could mistake for
"fetch everyone": no envelope in the lake for `(season, week)` at all
(`scope_unavailable`), and an envelope that exists but resolved zero members
(`scope_empty`) — a `roster-scope` capture against a dead upstream still
writes an envelope, and an empty one is not silently different from a missing
one to a collector reading it.

**There is no unnarrowed fallback.** A collector that caught
`ScopeUnavailable` and fell back to fetching every player anyway would spend
its entire per-run vendor-API budget in exactly the run where the fleet's own
scope is unavailable — an incident on `roster-scope` would then cascade into
every collector maxing out its own rate limit at the same time, which is the
one moment a shared vendor budget can least absorb it. The correct response to
`ScopeUnavailable` is the same shape `roster-scope`'s own ledger-unavailable
path uses (see
["A failed capture still writes an envelope"](#a-failed-capture-still-writes-an-envelope),
later in this doc): write a
`present: 0` envelope, classify the reason, make zero upstream calls, and let
next pass's alert be `collector_coverage_ratio` reading zero rather than a
vendor 429 that took the rest of the fleet down with it.

**`IdentityClient` resolves upstream rows to canonical `fdy-` ids, and never
adopts a refusal.** `player-identity`'s `POST /resolve/batch` is the
authoritative answer for "who is this" across the fleet, chunked at
`BATCH_LIMIT` (500) so a season's worth of queries never lands in a single
oversized request body. Its `resolve_many(...)` returns a mapping — **only**
the queries it
was told `resolved: true` for. Anything else (an absent `resolved` field, an
explicit `false`, `candidates` with no chosen winner) is simply missing from
the returned dict, exactly the way `roster-scope`'s own
`HttpPlayerIdentityResolver` treats its single-query `GET /resolve`: the
upstream's `candidates` are its own working, filed against its own
name-resolution-miss queue, not a second-guess opportunity for the caller. A
client that re-ranked `candidates` against a local confidence floor would be
adopting an identity `player-identity` deliberately declined to give it — see
that class's docstring in `services/roster-scope/roster_scope/adapters/identity.py`
for the full history of why that used to be a real bug. Treat every id
`resolve_many` did not return the same way `roster-scope` treats every
resolver refusal: a slot recorded in `coverage.missing` with the reason
attached, never a skipped row — a skipped row shrinks the numerator and the
denominator together and reads as perfect coverage.

Successful resolutions are cached **per `IdentityClient` instance**, keyed on
`(crosswalk_version, query)`. That key only pays for itself if the instance
outlives a single capture pass — **build one per collector process and reuse
it across every pass**, the same lifetime as the `httpx.AsyncClient` a
collector's `client_factory` already hands it. A fresh instance every pass
starts with an empty cache regardless of `crosswalk_version`, which makes the
version-keyed invalidation this cache exists for unreachable — every lookup
would miss on construction alone, version or no version. (An earlier revision
of this doc said the opposite — "construct one per capture pass" — which was
exactly backwards for this reason.) Unresolved queries are deliberately never
cached: a miss today may become a hit once `player-identity` republishes, and
caching the refusal would pin that gap in place until the process restarts.

**`crosswalk_version` has no source today.** The cache is keyed on it and
unit-tested against it, but nothing a caller can call gives it a value to
pass: `POST /resolve/batch`'s response is `{results, count, resolved_count,
unresolved_count}` — no timestamp, no version, nothing to key a cache
invalidation on. So today every long-lived `IdentityClient` either passes
`None` (functionally: cache forever, for the life of the process) or a
caller-invented constant that `player-identity` never confirms or refutes.
The parameter and the tests that exercise it (`test_a_new_crosswalk_version_
invalidates_the_cache`, `test_a_crosswalk_version_change_on_the_same_client_
invalidates_the_cache`) are correct as a mechanism — a real version, once one
exists, invalidates the cache exactly as designed — but the mechanism is
inert until `player-identity` exposes something to drive it with. That is not
this seam's gap to close: it would need a new field on the resolve response
or a new endpoint, and speculatively adding one here without `player-identity`
side to back it would be documenting a contract that does not exist. Until
then, treat `crosswalk_version` as forward-looking wiring, not a working
invalidation path.

---

## The five-route contract

`GET /health`, `GET /metrics`, `GET /catalog`, `GET /signals`,
`POST /refresh` — served identically by every collector from
[`collector_core/routes.py`](../libs/collector-core/collector_core/routes.py).
That uniformity is the whole extensibility mechanism: a generator that can
consume one collector can consume all of them.

- `/health` and `/metrics` are exempt from bearer auth (a kubelet probe and a
  Prometheus scrape cannot carry a token) and are deliberately **not** in
  `gateway.publicPaths`, so they answer in-cluster only.
- Every other route requires `Authorization: Bearer <token>`. An absent or
  empty `COLLECTOR_TOKEN` returns **503 on every data route** — it fails
  closed, so a Secret that never syncs is loud rather than an open collector.
- `GET /signals` applies `season`, `week` and `signal_type` itself and
  delegates the rest to your `signal_matches`. Query values arrive as
  **strings**; comparing them to an int row value silently matches nothing, so
  `str()` the row side.

An extra route is a plain `@app.get` in `main.py` after the
`build_collector_app` call. Reach the lake and the collector name through
`app.state.collector_spec`, never a module-level global — the routes must see
the same objects a capture just replaced. Then add the path to
`gateway.publicPaths` in your values file, or it works in-cluster and 404s at
the edge.

### `POST /refresh` returns 202 — accepted, not done

The capture is dispatched as a background task and lands in `CaptureState`
whenever it finishes. **A test that reads `/signals` on the next line is a race,
not a test.**

That race used to be won by accident: nothing in the capture path yielded to the
event loop, so the dispatched capture always completed before the next request
was serviced. Moving the lake write to `asyncio.to_thread` — mandatory, because
boto3 is synchronous — introduced a genuine suspension point and turned a latent
race into a CI failure in `weather`. Poll instead:

```python
def wait_for_signals(client, *, count, timeout=10.0):
    deadline = time.monotonic() + timeout
    body = {"count": 0}
    while time.monotonic() < deadline:
        body = client.get("/signals").json()
        if body["count"] >= count:
            return body
        time.sleep(0.05)
    raise AssertionError(f"capture did not land within {timeout}s: {body}")
```

Bounded, and loud on timeout with what it actually saw. The scaffolder generates
this; `libs/collector-core/tests/test_collector_routes.py`'s `_wait_until` is the
async equivalent. The fix belongs in the test, not in the contract.

---

## `coverage.expected` must never derive from what succeeded

This is the single most consequential thing in a collector, and the failure is
completely silent.

A collector that builds its expectation from the document it just fetched
reports a truncated upstream carrying 100 of 2,900 records as
`expected: 100, present: 100`, ratio **1.0** — perfectly healthy, while 96% of
the league vanished and the generator quietly trains on the hole.

[`CoverageAccumulator`](../libs/collector-core/collector_core/coverage.py) makes
the right shape the easy one:

```python
acc = CoverageAccumulator(floor=EXPECTED_FLOOR[signal_type])
for row in rows:
    key = row_key(row)
    acc.expect(key)          # because the row is OWED, never because it worked
    try:
        signals.append(build_signal(row))
    except Exception as exc:
        acc.fail(key, metrics.reason_for(exc))
        continue
    acc.record(key)          # only after it actually landed
```

- `expect` on the fact that made a key qualify. `record` only on success —
  it *refuses* a key that was never expected.
- `fail` declares the key expected as well: a failure is evidence the key was
  owed, which is the opposite of deriving expectation from success.
- `floor` is the size the universe is **known** to have — 32 teams, 272 games,
  416 scope slots, ~2,900 rostered players. Declare it as a constant. It never
  lowers a genuine count, so real expansion past the floor still reports
  honestly.
- Route pass-level problems through `acc.add_error(...)` so the error cap is
  applied in one place. The array is capped at 50 with an explicit
  `errors_truncated` marker — a silently truncated error list looks like a
  short list of problems.

`0/0` reads as ratio **1.0**, which is correct for a bye week and catastrophic
for a pass that captured nothing. That is why the floor is not optional.

## A failed capture still writes an envelope

The Phase 8 contract: *a poll that fails writes an envelope with
`coverage.present: 0` and a populated `errors` array*. A gap in the append-only
lake must be explicit, never inferred from absence — "we failed" and "we never
tried" are different facts.

[`fail_capture`](../libs/collector-core/collector_core/failure.py) is both
halves:

```python
try:
    rows = await fetch_rows(season, week, client=client, now=now)
except Exception as exc:
    await fail_capture(                    # writes, then RE-RAISES. Never returns.
        exc,
        collector=COLLECTOR_NAME, signal_types=SIGNAL_TYPES,
        adapter=UPSTREAM_ADAPTER, now=now, scope=scope,
        lake=lake, metrics=metrics, expected=EXPECTED_FLOOR,
    )
```

The **write** makes the gap explicit. The **re-raise** matters just as much:
`run_capture_loop` catches and leaves `CaptureState` untouched, so the last good
capture survives. Returning the failure envelopes normally would install them
over it, turning an upstream outage into a loss of *availability* on `/signals`
when the whole point of capturing into a cache is that an outage costs only
freshness.

Pass `expected=` so each signal type floors to its real number. Without it every
failure envelope floors to 1, and the coverage ratio reads better than it is.

**Do not call `metrics.capture_failure(exc)` before it.** `fail_capture`
records `collector_capture_failures_total` itself, once per failed pass. This
used to be the caller's job, which meant nothing in the library ever touched
the counter and every collector had to remember it on every failure path —
`weather` in five places, `roster-scope` in three, `player-identity` in three,
the library in none. A convention twenty-six authors must each remember is not
a guarantee.

You still record it yourself for a failure **the library cannot see**: one bad
row, one item's fetch inside a multi-call pass, or a degraded path that builds
its own envelopes instead of routing through `fail_capture` (`roster-scope`'s
`LedgerUnavailable` branch is the fleet's example).

## The success path ends in `publish_capture`, not a write loop

```python
return await publish_capture(envelopes, lake=lake, metrics=metrics)
```

[`publish_capture`](../libs/collector-core/collector_core/publish.py) writes
every envelope off the event loop, records every coverage gauge, records
`collector_capture_failures_total` if a write fails — and **returns the
envelopes anyway**. Do not hand-roll the loop it replaces.

That last part is the whole point, and it is the opposite of what
`fail_capture` does, deliberately. Nine collectors wrote their own tail and
eight let a failed `awrite` escape:

```python
for signal_type, envelope in envelopes.items():
    await awrite(lake, envelope)              # raises -> the envelopes are lost
    metrics.coverage(signal_type, envelope.coverage.ratio)
return envelopes
```

Every upstream fetch has already succeeded at that point. The envelopes are
built, correct, and in memory; only the durable copy failed. Letting it escape
means `_run_capture`/`run_capture_loop` catch it, `apply_capture` is never
reached, and `/signals` serves the previous capture — or nothing at all on a
first run. **An object-store outage cost availability**, which is exactly the
inversion the cache exists to prevent.

So: **availability wins over durability, and the durability failure is made
loud.** The lake is append-only and resolved by recency, so the next successful
pass writes a superseding object — a missed write is recoverable. Refusing to
serve data the collector already has is not, for every caller in the meantime.

`fail_capture` keeps the opposite answer because it is the opposite case: there
the *capture itself* failed, and installing its `present: 0` envelopes over the
last good ones destroys good data. "The capture failed" and "the capture
succeeded and only its archival copy failed" are different facts.

One consequence worth knowing: a collector that derives state from its own last
lake object (`player-stats`'s revision counter) can now serve an in-memory
envelope whose revision the lake does not have. That was already true — the
write failed either way — and the alternative is serving nothing.

---

## Memory: never buffer the same response twice

`roster-scope` was `OOMKilled` at a 256Mi limit on its first deploy — exit 137,
`CrashLoopBackOff`, probes reporting `connection refused` because the process
was simply gone. Its 171 tests passed and a local `docker run` was fine, because
neither had a memory limit. The cause was a 36.8 MB upstream document held three
times over.

Two rules:

1. **Never hold an upstream response in memory more than once.** `resp.text`
   plus a decode plus an `io.StringIO` handed to `csv` is three copies. Parse
   straight off the response.
2. **Filter as you parse, not after.** `roster-scope` read 300,000 rows and kept
   983; materialising the other 299,017 first was pure waste.

[`collector_core.streaming.stream_csv_dicts`](../libs/collector-core/collector_core/streaming.py)
is rule 1 made reusable and hands rows out one at a time so an ordinary
`continue` satisfies rule 2. Raising the memory limit is the wrong fix and was
deliberately reverted: it hides the bug and re-sizes the pod against an upstream
nobody controls.

## The lake refuses to run on the event loop

`LakeWriter` is synchronous boto3. `build_collector_app` hands every collector
an `EventLoopGuardedLake`, which **raises** if a synchronous method is called
from the loop thread. Use
[`awrite` / `aread` / `alist_keys`](../libs/collector-core/collector_core/lake.py),
or `asyncio.to_thread` around a whole synchronous helper.

The guard exists because the alternative failure is invisible: a blocking call
there does not error, it stalls the entire process — including `/health` —
until the kubelet kills the pod, and `connection refused` looks nothing like
its cause. `roster-scope`'s ledger read was its capture's *first* statement with
no `await` before it, so the task ran start-to-finish on the loop before uvicorn
finished starting, `/health` never answered, and `kubectl rollout status` timed
out at 180s. botocore's defaults make the worst case minutes: a 60-second
connect timeout, retried.

A useful side effect: your capture's first `await` should come early, so uvicorn
can finish starting before any upstream or object-store latency is incurred.

---

## What you actually have to write

The scaffolder leaves five TODOs, and they are the whole job:

1. **`adapters/upstream.py`** — set `UPSTREAM_URL`, delete the placeholder
   branch, parse the real wire format. Keep the adapter the *only* module that
   knows it.
2. **`capture.py`'s `EXPECTED_FLOOR`** — the real size of your universe, per
   signal type. Not a count of anything you fetched.
3. **`capture.py`'s `build_signal`** — your collector's actual product. Mirror
   its shape in `contracts/signal-envelope/collectors/<name>.json`;
   `tests/test_capture_contract_conformance.py` validates the **real** output of
   that function against the schema, so a renamed field fails there rather than
   in the generator six weeks later.
4. **`metrics.py`** — your own series, or delete the subclass and use
   `CollectorMetrics(COLLECTOR)` directly, as `weather` does.
5. **`helm/values/<name>/values.yaml`'s `CAPTURE_ENABLED`** — scaffolded
   `false`. A dispatched `POST /refresh` reaches the upstream regardless of this
   flag, so a smoke hook for a `false` collector must not post one.

`signals.py`'s `SUPPORTED_FILTERS` needs attention too: do not declare a filter
you have not implemented in `signal_matches`. The router will accept it, the
predicate will ignore it, and the response returns everything — which looks
exactly like a working filter.

---

## Registry, merge order, and CI

Your registry entry lands in the **same PR** as the service, and **after** the
PRs that added its `depends_on` entries. Both are enforced by
`tests/test_collector_registry.py` rather than by anyone remembering. The file
is append-only and unsorted on purpose, so two collectors on two branches merge
as a plain append.

`envelope_version` is the **string** `"1"`, quoted. Both gates compare exactly;
`1` against `"1"` is drift.

`scope_aware` is type-checked as a bool and nothing else. No code representation
of it exists, so its correctness is human-reviewed — a green run says nothing
about whether it is right.

Before opening a PR:

```bash
cd services/<name> && uv run pytest -v
uv run --with pyyaml==6.0.3 --with pytest --with jsonschema pytest tests/ -q
uv lock --check
docker build -f services/<name>/Dockerfile -t <name>:local .
```

The Docker build is not optional and no pytest run substitutes for it: **pytest
never touches a Dockerfile**, and adding a workspace member has broken an
*unrelated* service's image before.

Do not deploy to a shared Kind cluster that ArgoCD manages —
`deploy-local.py` hits a Server-Side Apply conflict, and forcing it or
disabling `selfHeal` fights the controller. See CLAUDE.md's ArgoCD section.

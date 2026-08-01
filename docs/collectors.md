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
`helm/values/*`), a `telemetry.py`, an `auth.py`, a `scheduler.py`, a
`Dockerfile` (the root `Dockerfile.collector` builds every collector, and
`tests/test_dockerfile_workspace.py` fails if a per-service one reappears), or
an edit to `deploy-local.py`, `stack-up.py`, `smoke-test.sh` or
`integration-test.yml`.

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
`collector_upstream_unchanged_total`, `collector_coverage_ratio`,
`collector_staleness_seconds`,
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

Three collectors narrow today — `usage-share` and `player-stats` against
`roster-scope`'s membership list, `injury-report` against membership ∪ matchup —
so this is a pattern to follow rather than a plan. `--scope-aware` on the
scaffolder still only records the flag on your registry entry; it does not
generate the wiring below, so you write that yourself, out of three pieces that
all live in `libs/collector-core` and are unit-tested independently of any
collector that calls them:
[`collector_core/scope.py`](../libs/collector-core/collector_core/scope.py)
(`ScopeClient`, and `fetch_scope_or_fail`, the fail-closed call site) and
[`collector_core/identity.py`](../libs/collector-core/collector_core/identity.py).

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

**`fetch_scope_or_fail` is that response, and you should not hand-roll it.**

```python
from collector_core.scope import fetch_scope_or_fail

failure_context = dict(
    collector=COLLECTOR_NAME, signal_types=SIGNAL_TYPES,
    adapter=UPSTREAM_ADAPTER, now=now, scope=scope, lake=lake,
    metrics=metrics, expected=EXPECTED_FLOOR,
    source_ref=source_ref(season, week),
)

# BEFORE the first upstream request. That ordering IS failing closed.
scope_members = await fetch_scope_or_fail(
    lambda: fetch_watchlist(lake, season, week), **failure_context
)
```

It takes a zero-argument callable, not an awaitable, so a *synchronous* raise
counts too — `usage-share` builds its `IdentityClient` inside the callable
because a missing `PLAYER_IDENTITY_URL` narrows to nothing just as completely as
a missing scope does. It returns whatever the callable returned (a `Scope`, a
`frozenset`, a tuple), and on failure it writes and re-raises rather than
returning. Do not pass `reason=` in `failure_context`; the helper supplies its
own.

**It has two `except` arms, and the second one is the one that gets dropped.**

* `ScopeUnavailable` → `reason=exc.reason` is forwarded rather than flattened
  to a literal. `scope_unavailable`, `scope_empty` and a collector's own
  `identity_unavailable` have three different fixes, and collapsing them costs
  an operator the only thing the envelope could have told them.
* **Everything else** → `fail_capture` with no `reason`, letting the shared
  classifier label it `malformed` or `unknown`. `ScopeUnavailable` is only what
  `ScopeClient` raises when the lake **answered** and held nothing usable; the
  lake can also fail *outright*, and `S3LakeWriter.list_keys`/`read` propagate
  botocore and JSON-decode errors untouched while `_parse_captured_at` raises
  `ValueError` on a timestamp it does not recognise. Omit this arm and every one
  of those escapes the capture coroutine: **no `present: 0` envelope, no
  `collector_capture_failures_total`, just a log line.** The failure is silent,
  which is why the arms live in the library instead of in a comment block each
  collector copies.

Whichever reason ends up on the envelope also becomes the
`collector_capture_failures_total` label, so a fail-closed pass is alertable as
`scope_unavailable` rather than landing in `unknown` beside genuine crashes.

**What a dropped row is, and is not.** An out-of-scope player is not a hole —
it was never owed, so it does not belong in `coverage.missing`, and recording
one would make narrowing read as a permanent coverage regression that buries the
rows that genuinely failed. `coverage.expected` stays the declared floor. It
must **never** derive from the scope size: `Coverage.ratio` returns 1.0 when
`expected` is 0, so a truncated scope naming two players would otherwise read as
a perfect week.

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

**A request that fails is not the same fact as a refusal, and `resolve_many`
will not tell you which you got unless you ask.** A chunk `player-identity`
could not be reached for (a 401, a connection refusal, a timeout) is caught per
chunk and recorded on `IdentityClient.failures` as `query -> reason`, and the
loop moves on — one dead chunk must not discard the chunks that already
resolved. Those queries are simply **absent from the returned dict**, exactly
like a genuine `resolved: false`. A caller that reads only the dict therefore
reports a full `player-identity` outage as an ordinary short week, whose
`errors` array says nothing but `below_expected_floor` — indistinguishable from
a two-member scope or a truncated feed. Read `failures` after each call (it is
reset at the top of the next one) and file **one summarised** entry per pass:

```python
if failures.rows:
    acc.add_error("identity_upstream_error", failures.detail())
```

Summarised, not per row: a ~1,700-row feed against a dead seam would otherwise
fill `CoverageAccumulator`'s 50-entry cap by itself and push every other reason
off the list. `usage_share/adapters/scope.py`'s `IdentityFailures` is the model.

Successful resolutions are cached **per `IdentityClient` instance**, keyed on
the query alone — **build one per collector process and reuse it across every
pass**, the same lifetime as the `httpx.AsyncClient` a collector's
`client_factory` already hands it. A fresh process starts with an empty
cache. Unresolved queries are deliberately never cached: a miss today may
become a hit once `player-identity` republishes, and caching the refusal
would pin that gap in place until the process restarts.

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
- Use `acc.add_priority_error(...)` — sparingly — for the one entry that
  *explains* the pass, such as "every row was resolved and then dropped by the
  scope". It inserts at the front so the cap cannot delete it, the same reason
  `errors` already prepends `below_expected_floor`. Do not reach into
  `acc._errors` to do this by hand; that is a private attribute of a library
  class, and at twenty-six collectors what gets copied is the workaround.

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

### If you gate state on the write, ask `PublishResult.landed`

Swallowing the failed write is right for availability and **wrong for any
collector that keeps state gated on the write having landed**. The case that
forced this into the library is the digest gate — the pattern that suppresses a
byte-identical append:

```python
if _PUBLISHED_DIGESTS.get(key) == digest:
    raise UpstreamUnchanged(...)     # nothing changed; do not re-append
...
_PUBLISHED_DIGESTS[key] = digest     # ← WRONG: records even if the write failed
```

Record a digest for content the lake never received and the next pass digests
the same content, matches, raises `UpstreamUnchanged`, and **the object is never
written again until the upstream data itself changes**. On a `static reference`
or `seasonal` cadence that is months. Reproduced on `venue`: pass 1 with a
failing lake wrote nothing, pass 2 with a healthy lake raised
`UpstreamUnchanged` and still wrote nothing. Note it takes **two passes** to
see — the single-pass availability test stays green throughout.

`publish_capture` returns a `PublishResult`, a `dict[str, Envelope]` subclass
that also reports which writes failed:

```python
published = await publish_capture(changed, lake=lake, metrics=metrics)

for signal_type in published:
    if published.landed(signal_type):
        _PUBLISHED_DIGESTS[(season, week, signal_type)] = digests[signal_type]
```

Iterate the result, not your own `changed` or `envelopes` dict — `landed`
**raises** for a signal type the call never published, because the caller has
confused "unchanged, so not published" with "published and failed" and both
plausible defaults hide that. `True` records a digest for content never offered
to the lake; `False` reads as an outage that did not happen.

**That raise costs the pass.** It fires after every write has already happened,
no collector catches it, and `_run_capture`/`run_capture_loop`'s blanket handler
drops the capture — so `/signals` keeps serving the previous one and
`last_capture_at` stops advancing toward a staleness alert, *even though the
lake write succeeded*. Looping over your own `envelopes` dict instead of
`published` is the natural slip (both are in scope; `published` is the
newcomer), and nothing in CI catches it for you.

Gate **per signal type**, not per pass. `if any(published.landed(st) for st in
published)` looks equivalent and is not: one type's write failing while the
others land records the failed type's digest anyway, and it is never written
again. It takes a *partial* lake failure to see — a total-outage test passes
either way, because with nothing landing the two gates agree. All three
collectors carry a fixture per arm for exactly this reason.

`venue`, `player-profile` and `durability-history` each carried a private
`_WriteObserver` wrapper doing this before it moved here. **Do not write a
fourth.** If you find yourself wrapping the lake to learn what `publish_capture`
already knows, the answer is on the return value.

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

### A gzipped upstream: `gzipped=True`

For an upstream published as a `.csv.gz` **artifact** — the compression is part
of the file, not of the transfer, so httpx neither does nor can inflate it and
`aiter_text()` on one yields mojibake. nflverse's play-by-play release is the
case that forced it: **93.4 MiB** as `.csv`, **18.2 MiB** as `.csv.gz`, and
`officiating` polls it daily.

```python
async for row in stream_csv_dicts(client, url, gzipped=True, etag_key=url):
    ...
```

Three things it gives you that hand-rolling does not, all of them silent
failures otherwise:

- **Peak memory is bounded here, not by the transport.** A gzip stream expands
  ~5x, so handing each network chunk straight to `decompress()` makes peak
  memory a property of whatever chunk size the transport chose. Measured: a
  buffered 18.2 MiB body became one 93.4 MiB string and peaked at **281 MiB
  against a 256Mi limit** — `roster-scope`'s OOMKill, rediscovered. The shipped
  path caps each inflation step at `MAX_INFLATED_CHUNK`.
- **A truncated body raises `UpstreamTruncated`.** This is the one that matters
  most. Mid-stream corruption raises on its own (zlib checks the CRC);
  truncation does not — before the check, a body cut in half returned **9,895
  of 20,000 rows with no exception** and a fragment for a final row. A short
  document is a *plausible* answer, so coverage reports it as a genuinely quiet
  week rather than a transport failure, and it lands in an append-only lake.
- **An incremental UTF-8 decoder**, because a chunk boundary lands mid-codepoint
  eventually and a per-chunk `bytes.decode()` raises only on the documents
  unlucky enough to contain one.

`UpstreamTruncated` subclasses `ValueError`, so `reason_for` already classifies
it `malformed` and it needs no new metric label.

### A wide upstream: `columns=`

Rule 2 applied to **width** rather than length. Play-by-play carries 372
columns and `officiating` reads six of them; building the other 366 into a dict
for each of 48,771 rows measured **2.8s** of CPU per pass against **1.5s**
(median of three runs through the shipped path against the real artifact).

```python
COLUMNS = frozenset({"game_id", "season_type", "play_type", "penalty", ...})

async for row in stream_csv_dicts(
    client, url, required_columns=COLUMNS, columns=COLUMNS, gzipped=True
):
```

`columns=` narrows the **row dicts**; `required_columns=` still validates the
full header, so projecting a column away does not stop schema-drift detection
from noticing it disappeared. A name in `columns` that the header does not
carry is simply absent from the rows — say `required_columns` when you need it
to exist. This buys CPU and allocation churn, not headroom: rows are yielded
one at a time either way.

### Before format, check FRESHNESS: an artifact can be abandoned

**Compare `updated_at` across an nflverse release's formats before you compare
their sizes.** They are built by separate steps and one can silently stop being
regenerated while the release around it is rebuilt daily. When that happens the
size rule below does not apply at all — the choice is between a live document
and a dead one, and there is nothing to trade off.

    curl -s https://api.github.com/repos/nflverse/nflverse-data/releases/tags/<tag> \
      | python3 -c "import json,sys; [print(a['name'], a['updated_at']) for a in json.load(sys.stdin)['assets']]"

`contracts` is the case that found this, on 2026-08-01:

| asset | updated |
|---|---|
| `historical_contracts.csv.gz` | **2022-05-29** |
| `historical_contracts.parquet` | 2026-08-01 |
| `historical_contracts.rds` | 2026-08-01 |
| `timestamp.json` | 2026-08-01 |

The CSV had not been rebuilt in four years. Its newest `year_signed` was 2022
and **2,869 of its 2,887 "active" contracts had already expired**; the parquet's
newest was 2026. Nothing raises on a stale document — `player-contract` read the
CSV, passed 146 tests and published four-year-old contracts as current. So it
reads the parquet, with `pyarrow` in **its own** `pyproject.toml` and nowhere
else, and `docs/architecture/phase-8-data-source-collectors.md` records the
deviation.

**The whole dependency set was audited when this surfaced** — pbp,
ftn_charting, pbp_participation, officials, players, snap_counts, injuries,
combine, depth_charts and rosters. `contracts` is the **only** release with
format-divergent staleness; everywhere else every format shares a timestamp. So
this is a landmine rather than a pattern, and the rule below stands unchanged
for every other feed.

Two things keep the exception from becoming the rule. `pyproject.toml` is
per-service, so a wheel added for one collector is not added to the fleet. And
parquet's footer-at-the-end layout still costs a full buffer before the first
row — tolerable at 6.44 MiB, not at 93 MiB. A collector taking this exception
must also filter in Arrow *before* `to_pylist()`; see
`services/player-contract/player_contract/adapters/upstream.py`, where doing it
the other way measured 157.6 MB peak RSS against a 268 MB limit.

### Format: take `.csv.gz` where it exists, plain CSV otherwise — not parquet

**Decided once, fleet-wide, with live measurements. Do not re-litigate it per
collector** — but do check the freshness question above first, because this
rule assumes both formats are current. nflverse publishes most release assets
four ways, and the parquet variants look dramatically smaller until you check
the one that matters. Measured over the wire on 2026-08-01 with
`Accept-Encoding: gzip` offered:

| artifact | csv | csv.gz | parquet |
|---|---|---|---|
| `play_by_play_2025` | 93.41 MiB | **18.22 MiB** | 19.40 MiB |
| `ftn_charting_2025` | 7.75 MiB | *(none published)* | 0.53 MiB |
| `pbp_participation_2025` | 46.82 MiB | *(none published)* | 4.52 MiB |

**On play-by-play — the document most collectors reach for — the parquet is
LARGER than the gzipped CSV.** Parquet's win exists only on the assets
nflverse does not gzip. Against that, `pyarrow` is a 47.8 MiB cp312 manylinux
wheel (>100 MiB installed) added to *every* collector image, and parquet's
footer-at-the-end layout means the body must be buffered before any row can be
read — reversing the streaming rule that fixed `roster-scope`'s OOMKill. So:

- take `.csv.gz` with `gzipped=True` where nflverse publishes one;
- take plain CSV with `columns=` otherwise;
- **do not add `pyarrow` to the fleet.**

`gzipped=True` also buys a correctness property the plain-CSV path cannot have:
a gzip member carries a trailer, so a short body raises `UpstreamTruncated`
rather than silently yielding half a document.

**When a big ungzipped feed really is the problem, the cheap fix is not a new
dependency — it is asking what the feed buys.** `team-scheme` reads all three
of the above: 73.28 MiB a pass, of which `pbp_participation` alone is 46.82 MiB
(64%) and buys exactly **one field of thirteen** (`personnel_rates`). 85% of
the total available parquet saving is that single feed. Dropping or
de-cadencing it costs one field and no dependency; adding `pyarrow` costs a
fleet-wide wheel and keeps the feed. Measure the field-per-megabyte before
reaching for a format change.

Revisit this rule only for a collector needing an nflverse asset that (a) has
no `.gz` variant, (b) exceeds ~40 MiB, **and** (c) ships
`CAPTURE_ENABLED=true`. All three, not any one — a large feed behind a
disabled loop costs image size and no bandwidth.

That test is about **size**, and it is not the only way past this rule. A
`.csv.gz` that the upstream has stopped regenerating fails the rule's own
premise rather than its threshold — see the freshness section above, and
`player-contract` for the one collector that takes that exception.

**Scope of the `play_by_play` finding:** it settles the pbp document for every
collector that reads it, `defense-vs-position` included. It does **not** settle
a collector that also wants `pbp_participation` — that feed meets the 46.82 MiB
question on its own terms, and the answer there is the field-per-megabyte one
above rather than this one.

## Conditional GET: skip a re-fetch the upstream itself says is unnecessary

**Opt-in, one argument wide, for an upstream that changes slower than your
poll interval.** `depth-chart`'s season CSV is republished far less often than
a `volatile`-cadence collector polls it — how much less is not measured here,
so no number is claimed — and a poll landing between two republications spends
its whole budget re-downloading bytes it already has.
[`collector_core.conditional`](../libs/collector-core/collector_core/conditional.py)
owns the protocol: `ETagStore`, the shared `ETAGS` instance, and
`conditional_stream`, which is the only supported way to speak it. There are
two ways to *drive* it, depending on how your adapter reads the upstream.

**Route 1 — your adapter already calls `stream_csv_dicts`.** Pass
`etag_key=<the URL>` and add `except UpstreamUnchanged: raise` **above** your
generic `except Exception` handler:

```python
rows = stream_csv_dicts(
    client, url, required_columns=REQUIRED_COLUMNS,
    etag_key=url,   # the cache key — use the same string the envelope
)                   # records as upstream.source_ref, so they cannot drift
...
try:
    ...
except UpstreamUnchanged:
    raise                          # above the generic handler, unchanged
except Exception as exc:
    await fail_capture(exc, ...)
```

`depth-chart` (`depth_chart/adapters/upstream.py`) is this route.

**Route 2 — your adapter streams the response itself**, the way
`roster-scope` folds the same feed into per-team charts without going through
`stream_csv_dicts`. Use
[`conditional_stream`](../libs/collector-core/collector_core/conditional.py)
in place of `client.stream`, read `stream.response` however you like, and call
`stream.commit()` as the **last** statement — after the trailing row, after
every schema check, after anything that can still fail. That is the whole
difference from `client.stream`; no snippet is reproduced here on purpose.

`roster-scope` (`roster_scope/adapters/depth_chart.py`) is this route, because
its adapter consumes neither `stream_csv_dicts` nor `fail_capture` directly.

**Do not hand-roll the protocol.** It looks like four lines — headers, a 304
branch, `raise_for_status`, `ETAGS.set` — and two of them are wrong in ways no
test in this repo would catch if you wrote them again in a service tree:

- **The `304` check is required, not defensive.** `raise_for_status()` gates on
  `is_success`, which is 2xx only, so a `304` **raises `HTTPStatusError`** like
  any other non-2xx. Drop the check and every unchanged upstream becomes a
  capture failure that writes `present: 0` over healthy data. (An older
  revision of this page said the opposite — that httpx only errors on 4xx/5xx
  and the check was belt-and-braces. It was wrong; `test_httpx_raise_for_
  status_rejects_a_304` in collector-core pins the real behaviour.)
- **The ETag is committed *after* the body is read, never on the response
  headers.** An ETag claims you hold the whole document. Commit one for a body
  that died at 30 MB of 37 and every later pass 304s: `mark_unchanged` advances
  `last_capture_at`, staleness resets to ~0, the failure counter stops moving,
  and the collector reports itself healthy on a truncated document until the
  upstream republishes — hours to days. A loud, self-retrying failure becomes
  a silent, sticky one.

`conditional_stream` makes the second one unrepresentable by never committing
for you, and `stream_csv_dicts` commits from its generator tail, which a
`break` or an exception cannot reach.

**Both halves are required either way.** Without the `etag_key`/`ETagStore`
half, nothing is saved — every poll still round-trips the full body. Without
the `except UpstreamUnchanged: raise` half placed *above* the generic
exception handler, a `304` is routed into `fail_capture`, which writes a
`present: 0` envelope over a healthy capture and counts a failure that did not
happen.

A `304` is a **successful** capture, not a skipped one. `run_capture_loop`
and `_run_capture` catch `UpstreamUnchanged` and call
`CaptureState.mark_unchanged(now)`, which advances `last_capture_at` and
records `collector_upstream_unchanged_total` without touching the stored
envelopes. So `/catalog` reports a fresh pass while `/signals` keeps serving
the previous capture's rows unchanged — that is the two fields meaning
different things, not drift.

Do not opt in a collector whose upstream is generated per request or otherwise
lacks a stable `ETag`/`Last-Modified` — a value that changes on every poll
costs one extra round trip on the conditional request and saves nothing.

### Caveat: `POST /refresh` with a different scope can 304 into a no-op

`POST /refresh` accepts a `season`/`week` override, so an operator can
backfill a week outside the cadence. The ETag store knows nothing about that:
it is keyed by **URL**, and neither collector's URL varies by week — the feeds
are season-scoped current snapshots. So `POST /refresh {"week": 5}` against an
already-fetched season sends `If-None-Match`, gets a `304`, and takes the
healthy-pass branch: no envelope is written for week 5, `last_capture_at`
advances, and the route already returned `202`. **The operator gets every
signal of success and no data**, with nothing in the metrics to distinguish it
from an ordinary unchanged poll. Confirm a backfill by reading `/signals` for
the week you asked for, not by the `202`.

This is a real gap, stated rather than papered over. Plumbing an
ignore-the-cache flag from the refresh body through `capture` into the adapter
is the structural fix and is deliberately **not** done here — it touches every
adapter signature. Until then, the operator workaround is a pod restart: the
store is in-memory, so a restart costs exactly one full download per key and
makes the next capture unconditional. A collector whose upstream URL *does*
vary by week is unaffected, because the week is already part of the key.

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

   **This is the one TODO with a gate behind it.** The scaffolded schema and
   the scaffolded `build_signal` are written to agree with each other, so that
   conformance test proves the two *match* — not that either is *right*. Skip
   this step and you ship a schema validating nothing meaningful, and every
   test stays green. So each generated signal type carries a
   `"$comment": "TODO(new-collector): ..."`, and
   `tests/test_placeholder_schemas.py` (in `platform-tests`) fails on any
   committed collector that still has it — or that has had the marker deleted
   while keeping the placeholder's `key`/`observed_at`/`value` field set.

   It is a marker rather than a generated failing test on purpose: a scaffold
   that is red the moment you run `uv run pytest -v` teaches you that red is
   normal. Rewriting the schema is also what drags `build_signal` along, since
   the conformance test validates one against the other.
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

`scope_aware` is checked structurally by `tests/test_scope_aware_gate.py`:
declaring `true` requires importing `collector_core.scope.ScopeClient`
somewhere in the collector's source, read by AST like the rest of the
registry gate — the check is `ast.ImportFrom` only, so `import
collector_core.scope` followed by attribute access (`collector_core.scope.
ScopeClient(...)`) will not satisfy it; use `from collector_core.scope import
ScopeClient`. That proves the narrowing seam is wired in, not that it behaves
correctly — whether a capture actually fails closed and drops out-of-scope
rows is proven behaviourally by the collector's own test suite, the same way
every other behavioural claim in this guide is.

Before opening a PR:

```bash
cd services/<name> && uv run pytest -v
uv run --with pyyaml==6.0.3 --with pytest --with jsonschema pytest tests/ -q
uv lock --check
docker build -f Dockerfile.collector \
    --build-arg SERVICE=<name> --build-arg PACKAGE=<pkg> --build-arg PORT=<port> \
    -t <name>:local .
```

Run that from the repo root, not the service directory: a collector depends on
the `libs/collector-core/` workspace member by path, so the root **is** the
build context. `<port>` is `service.port` from `helm/values/<name>/values.yaml`
— the artifact Kubernetes actually applies, which is why the port the container
listens on cannot drift from the one the probes dial.

The Docker build is not optional and no pytest run substitutes for it: **pytest
never touches a Dockerfile**, and adding a workspace member has broken an
*unrelated* service's image before.

Do not deploy to a shared Kind cluster that ArgoCD manages —
`deploy-local.py` hits a Server-Side Apply conflict, and forcing it or
disabling `selfHeal` fights the controller. See CLAUDE.md's ArgoCD section.

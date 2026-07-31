# Collector cost controls, then scope narrowing — design

**Status:** approved, not implemented
**Date:** 2026-07-31
**Supersedes the framing of:** `foundry-handoff-2026-07-31.md` Part 2 ("Plan B,
and its three prerequisites")

Companion to [`2026-07-30-scope-narrowing-design.md`](2026-07-30-scope-narrowing-design.md),
whose five decisions this document does not revisit.

---

## Why this document exists, and why it is not "Plan B"

The handoff planned a pilot: wire one collector (`usage-share`) onto the Plan A
seams, then repeat for five more. Three "prerequisites" were named as blockers.

Two of the three turned out not to be blockers, and a fourth problem that
nobody had filed turned out to be the expensive one.

| Handoff prerequisite | Finding |
|---|---|
| `crosswalk_version` has no source | Not a blocker. It is a cache key on an in-memory cache nothing populates. **Delete the field.** |
| `roster-scope` ships `CAPTURE_ENABLED: "false"`, so CI has no scope | **Real.** Addressed in Part 2. |
| Reverse `fdy-` → native id lookup | Not a Plan B problem. `usage-share` narrows *forward* (see Part 2). The reverse map is an 8C concern and is deferred to 8C. |
| *(unfiled)* Bulk upstreams re-downloaded on a 15-minute poll, into a lake that never prunes | **The actual cost driver.** Part 1. |

Ordering follows from that: **cost controls ship first**, because narrowing
depends on none of them and does not fix them.

---

## The cost picture, measured

Nothing costs money today — the cluster is Kind, and only `weather` reaches a
third party at all (Open-Meteo, ~32 stadiums of small JSON). `injury-report`
has `CAPTURE_ENABLED: "true"` but ships both upstream URLs **empty**, which
selects an in-process stub, so it reaches nobody. The other seven collectors
have capture off.

This becomes real at Phase 6 (EKS). Two mechanisms, both measured rather than
estimated:

**1. Bulk assets re-downloaded whole, on a short cadence.**
`depth-chart` declares `cadence_class: volatile` = `timedelta(minutes=15)` =
96 captures/day. Live `Content-Length` measurements, 2026-07-31:

| Asset | Size |
|---|---|
| `depth_charts_2025.csv` | 52.9 MB |
| `depth_charts_2026.csv` — **live**, `CAPTURE_SEASON: "2026"` | **37.1 MB** |
| `depth_charts_2024.csv` | 3.4 MB |

At the live figure that is **~3.4 GB/day, ~107 GB/month**, re-downloading a
file that changes a few times a week. It is one `CAPTURE_ENABLED` flip away.
`helm/values/depth-chart/values.yaml` already cites the 2025 number and the
~5 GB/day it implies; this design uses the live 2026 asset instead.

**And it is fetched twice.** `roster_scope/adapters/depth_chart.py` and
`depth_chart/adapters/upstream.py` resolve to the identical URL template, so
`roster-scope` and `depth-chart` pull the same 37.1 MB asset independently —
open issue **#82**. Decision 1 makes the duplicate nearly free; it does not
make it disappear, and deduplicating the fetch remains the better long-term
answer.

**2. `cadence_class: weekly` polls *daily*.**
`BASE_INTERVALS[WEEKLY] = timedelta(days=1)`
(`libs/collector-core/collector_core/cadence.py`). So `usage-share`,
`player-stats` and `roster-scope` each re-pull their whole file every 24 hours
— 7× what the label implies. Individually small (8.3 / 8.3 / 37 MB), and
defensible as a way to catch upstream revisions, so **this design does not
change the cadence classes.** Part 1 makes the daily poll nearly free instead.

**3. The lake never prunes.** `collector_core/lake.py` is append-only by design
("Objects are never mutated or deleted in place") and there is no lifecycle
rule, expiry or prune anywhere in the repo. Every capture writes a new
envelope forever. At volatile cadences across a 26-collector fleet that is
thousands of objects a day, most of them byte-identical to their predecessor.

Storage per GB is cheap. *Never deleting* is what turns it into a bill.

---

## Decision 1 — Conditional GET, cadences unchanged

**Send `If-None-Match` with the previously stored ETag. On `304`, skip the
download and write no envelope.**

This attacks both mechanisms at once and gives up nothing:

- `depth-chart` keeps its 15-minute freshness — a real change is still noticed
  within 15 minutes — while 95 of every 96 daily polls cost a few hundred bytes.
- The lake stops accumulating identical snapshots, because "unchanged" writes
  no object. That is a fix at the source, strictly better than deleting the
  duplicates afterwards with a lifecycle rule.

**Half the machinery already exists and is unused.** `player-identity`'s
Sleeper adapter already reads `response.headers.get("etag")` and stores it in
the envelope as `upstream.source_ref`, documented there as "the upstream's own
opaque cursor". Nothing ever sends it back. There is no `If-None-Match`
anywhere in the repository.

### Verified against the real upstreams, not assumed

This design rests on the claim that the actual feeds honour conditional
requests. Measured 2026-07-31 against the live endpoints:

| Upstream | ETag served | `If-None-Match` result |
|---|---|---|
| `raw.githubusercontent.com/.../games.csv` | `"2a243f7aa649…"` | **304** |
| nflverse release asset, `depth_charts_2026.csv` (302 → Azure blob) | `"0x8DEEEE7C68C0181"` | **304, 0 bytes downloaded** |

Control: the same 2026 asset requested *without* a conditional header returns
`206` for a ranged request, so the 304 is caused by the header and not by a
dead URL. The GitHub release URL redirects (302) to Azure blob storage and the
ETag comes from that final host; the redirect does not defeat the mechanism.

**Rejected alternative — just lengthen the cadences.** Fewer moving parts and
nothing to verify against a third party, but it buys the saving by giving up
freshness: a depth-chart change would go unnoticed for hours, which is the one
thing a `volatile` collector exists to avoid. It also does nothing about the
lake growing without bound.

**Rejected alternative — conditional GET plus a platform test forbidding a
`volatile` cadence against a large upstream.** The guard is the durable part,
and is worth revisiting once 8C lands. Deferred here to keep the change small.

### Where it lives

In `collector_core`, once. Nine collectors today and twenty-six planned; a
per-adapter copy is precisely what Wave 0 spent its effort deleting, and
`tests/test_new_collector.py` exists to keep such copies from growing back.

### Mechanism

1. The capture path remembers the last response's ETag on `CaptureState`,
   in memory.
2. The next poll sends `If-None-Match: <etag>`.
3. `200` → today's path, plus storing the new ETag.
4. `304` → skip the parse entirely, write no envelope, record
   `collector_upstream_unchanged_total`.

**In memory rather than read back from the lake.** A pod restart then costs
exactly one full download, which is far cheaper than a lake read on every
capture forever.

**It degrades to today's behaviour.** An upstream that sends no ETag leaves
`source_ref` as `None`, so no `If-None-Match` goes out and nothing changes.
The mechanism fails *open*, which is safe because the only thing at risk is
bandwidth — unlike scope narrowing, which must fail closed.

### A 304 counts as a successful capture

`last_capture_at` advances; no envelope is written.

Staleness should measure *how long since we confirmed this data is current*,
not *how long since we last wrote bytes*. The alternative makes a perfectly
healthy `depth-chart` climb toward a staleness alert precisely **because** its
upstream is stable, which is backwards.

Consequence to hold in mind: `/catalog`'s `last_capture_at` advances while the
newest lake envelope's `captured_at` does not. That is not drift, it is the
two fields meaning different things — and a lake consumer reading an older
`captured_at` is reading the truth, because the data genuinely is that old.

This brushes open issue **#77** (`collector_staleness_seconds` steps at capture
cadence instead of climbing). It does not close it.

---

## Decision 2 — Three collectors narrow; six do not, on the record

Reviewed all nine deployed collectors against their registry `scope_aware`
flags and the reasons recorded beside them.

### Narrowing

| Collector | Narrows to | Today |
|---|---|---|
| `usage-share` | membership scope | `scope_aware: false`; unnarrowed |
| `player-stats` | membership scope | `scope_aware: true`, but runtime narrowing ships **off** |
| `injury-report` | membership **∪ matchup** scope | `scope_aware: false`; unnarrowed |

**`injury-report` is the reason the matchup scope exists.** Its registry
comment argues against narrowing: "an opposing cornerback ruled out moves a
receiver's projection as much as the receiver's own hamstring does, and
defenders never appear on an offence-oriented watchlist at all." That is an
argument for reading `scope_matchup_weekly` — the 608-slot CB/S/LB/DL/OL list
`roster-scope` already publishes — **in addition to** `scope_membership_weekly`.
It is not an argument for fetching all ~1,700 players. `ScopeClient.fetch`
already takes a `signal_type`, so both lists are reachable with no new seam.

**`usage-share` narrows forward, and this is why no reverse map is needed.**
Stream the CSV, batch-resolve each row's `gsis_id` through `IdentityClient`,
keep the rows whose `fdy-` id is in scope. Because `gsis` is a published
crosswalk source, `player-identity` adopts the link exactly (Tier 1) with no
scoring at all.

Its registry comment currently claims narrowing is *impossible* — "roster-scope's
membership rows carry no name and no external id, so there is nothing to join a
GSIS-keyed feed onto." **That comment is stale.** It predates `IdentityClient`
and assumes the reverse direction. Remove it with the change.

**`player-stats` also moves onto the lake.** It reaches roster-scope over HTTP
today (`ROSTER_SCOPE_URL`, `player_stats/adapters/scope.py`), which contradicts
decision 5 of the narrowing design — the scope is read from the lake so a
`roster-scope` outage costs freshness, not the whole fleet. Migrate it to
`ScopeClient`. Its previous blocker (roster-scope and player-stats minting ids
from two different stub resolvers, so the id spaces did not intersect) is gone:
both resolve through the real `player-identity` now.

**For bulk CSV upstreams, narrowing saves memory and lake volume, not
requests.** The file is one download either way. Request-count and
vendor-billing savings land on 8C's per-player APIs. Stated here because the
handoff warns that someone will otherwise try to "optimise" a bulk fetch that
cannot be optimised.

### Not narrowing, each for a stated reason

| Collector | Why not |
|---|---|
| `weather`, `schedule-context` | Signals are keyed by venue/game/team. There is no player in them to narrow. |
| `player-identity` | It decides what a `player_id` *is*, and must resolve deep-bench names that books and news feeds mention. |
| `roster-scope` | It produces the scope. |
| `roster-transactions` | The players who matter are the ones **not** yet in scope — the transaction is what puts them there. Narrowing would filter out the signal itself. |
| `depth-chart` | The chart is a team-level structure; publishing only watchlist players hides the backup one move from entering it. It is also roster-scope's *input*, so narrowing it by roster-scope's output is circular. |

`roster-transactions` and `depth-chart` were considered for narrowing and
deliberately exempted. Both are bulk feeds, not per-player APIs, so Decision 1
covers their cost.

### CI gets its scope from a seeded fixture

`roster-scope` ships `CAPTURE_ENABLED: "false"` for a documented
third-party-load reason, so no scope envelope ever reaches the lake in CI —
and a narrowed collector then **correctly** fails closed and fetches nothing.
That is right behaviour that reads as a broken integration test.

Seed a fixture scope envelope into MinIO during `integration-test` setup.

Rejected: enabling capture in CI, and having the smoke test dispatch
`POST /refresh`. Both contradict decisions already written into
`helm/values/roster-scope/values.yaml`, which states outright that the smoke
test must not POST `/refresh` because a dispatched refresh reaches the upstream
regardless of the flag.

### `crosswalk_version` is deleted, not sourced

`player-identity` has no version concept: `resolve_queries` returns
`{results, count, resolved_count, unresolved_count}` and the word does not
appear in the service outside `CROSSWALK_KEYS`/`CROSSWALK_SOURCES`. Nothing
can populate the parameter, so the cache key it guards is inert.

Two live alternatives were available and both rejected as solving nothing
anybody needs: `GET /catalog`'s existing `last_capture_at` (for
`player-identity`, its only capture *is* the crosswalk rebuild), and the
`player_identity_crosswalk` envelope's own `captured_at` in the lake. Either
would work the day a caller genuinely needs cross-pass caching. None does
today.

Remove the parameter, its cache-key tuple, and the two tests that exercise it.

### #78 closes with a behavioural test

`scope_aware` is type-checked only — nothing verifies the value, as both the
registry and `CLAUDE.md` state. A collector declaring `scope_aware: true` must
**fail closed when no scope exists**: zero upstream calls, a `present: 0`
envelope. That is observable, so it can be asserted rather than reviewed.

---

## Testing

**Conditional GET.** `respx` returning `304`: assert no lake write occurred,
that `last_capture_at` advanced, and that the *second* request carried
`If-None-Match` matching the first response's ETag. An upstream sending no
ETag must behave exactly as today.

**Narrowing.** The load-bearing assertion is behavioural, not a flag: with a
scope containing N players and an upstream carrying many more, the collector
publishes only the N. And with **no** scope, it makes **zero** upstream calls.

**Mutation testing is mandatory** (lesson 1). Two pairings named up front:
making `304` write an envelope anyway must kill a test, and removing the scope
filter must kill a test. Per lesson 12, **every pairing in the implementation
plan must be verified empirically** and any that does not hold must be
reported rather than worked around. Pair every `all(...)`/`any(...)` over a
collection with a length assertion — `all([])` is `True`.

**Only a live container run finds the real bugs** (lesson 4). Every genuine
defect in 8A/8B came from running the image with the chart's real environment
under its real 256Mi memory limit. No unit test found any of them.

---

## Explicitly out of scope

- **The reverse `fdy-` → native id map.** Needed by 8C's per-player APIs,
  where request-count savings actually land. The data exists — every
  `player_identity_crosswalk` row in the lake carries `player_id` plus a full
  `external_ids` block for gsis, espn, yahoo, rotowire, sportradar and
  fantasy_data — but it is not reachable over HTTP (`candidate_payload`
  deliberately omits it). Design it at the start of 8C.
- **Changing any `cadence_class`.** Decision 1 makes the existing cadences
  cheap instead.
- **A lake lifecycle/retention rule.** Decision 1 stops most duplicate objects
  being written at all; revisit once its effect is measurable.
- **Issue #77** (staleness gauge). Brushed, not closed.
- **A platform guard forbidding a volatile cadence against a large upstream.**
  Worth revisiting once 8C lands.

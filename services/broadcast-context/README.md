# broadcast-context

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8022` |
| Gateway path | `/collectors/broadcast-context` |
| Cadence class | `weekly` |
| Signal types | `game_broadcast_window` |
| Capture loop | **on** (`CAPTURE_ENABLED=true`) — see [Cost](#cost) |

## What it captures

Whether a game is one of eight simultaneous 1 p.m. kickoffs or the only
football on television that night. The window matters because standalone
primetime games are played and officiated differently late, and because it
changes the pool of players a projection consumer is choosing between at that
slot.

Flex scheduling means this is not a season-start constant: a game's window can
change twice, and *when* it changed is part of the signal. **That history does
not exist upstream.** `games.csv` publishes the current schedule and nothing
else — no `flex_status`, no previous window, no announcement instant — so the
flex fields are derived by comparing each pass against this collector's own
append-only snapshots. On a first capture every game is `flex_status:
original`, which is correct; fabricating a flex history out of one fetch is the
failure this collector exists to avoid.

One upstream, measured live on 2026-08-01:

| Feed | Size | Carries |
|---|---|---|
| `games.csv` | **509 KB on the wire** (2.07 MiB parsed; httpx requests gzip) | the whole schedule since 1999, 272 rows for a scheduled season |

There is no second source. `games.csv` carries **no `network` and no `tv`
column** — checked, not assumed.

## The timezone decision: `is_primetime` and `kickoff_local_time`

The spec defines `is_primetime` as *"kickoff **local** time at or after
20:00"* and `kickoff_local_time` as the *"venue wall clock"*. The feed's
`gametime` is **Eastern for every game**, including the London 09:30 and the
Los Angeles 20:20.

Read literally, that makes a 20:20 ET Thursday kickoff in Los Angeles a 17:20
local kickoff and therefore *not* primetime — which is plainly not what anyone
means by primetime. Three options were available:

| | What it costs |
|---|---|
| **(a)** treat "local" as Eastern and say so | `kickoff_local_time` becomes a misnomer |
| **(b)** derive true venue-local time | an undeclared dependency on `venue` (a separate service and Python package, not in `depends_on`) — either reading its lake output or duplicating its committed 1,261-line stadium table |
| **(c)** `kickoff_local_time` null with a reason; `is_primetime` from Eastern | one honest null on a display field |

**(c) is what shipped**, and the argument for it is not merely that (b) is
expensive. It is that `is_primetime` and `kickoff_local_time` are asking two
different questions, and only one of them needs a venue:

* `is_primetime` is really "is this one of the national primetime packages",
  and the NFL **defines** those packages on the Eastern clock — TNF at 20:15
  ET, SNF at 20:20 ET, MNF at 20:15 ET, in every market. Computing it from
  Eastern is not an approximation of the spec's rule; it is the thing the
  spec's rule is a proxy for. Applying the 20:00 threshold to venue-local time
  would make every West Coast primetime game read `false`, which is the
  failure, not the fix.
* `kickoff_local_time` is a **display** field. It genuinely needs a venue
  timezone, and paying a cross-collector coupling for a display field is a bad
  trade. It is emitted as `null` with `null_field_reason:
  venue_timezone_unavailable`, and **`kickoff_eastern_time` is emitted
  alongside it, named for the zone it is actually in** — so nothing is
  mislabelled and a consumer that wants venue-local can join to `venue` itself
  using `kickoff_at`.

**Consequence worth knowing.** A 19:00 or 19:15 ET Monday game — the opener of
an MNF doubleheader week — is `is_primetime: false` under the spec's own 20:00
threshold while sitting in the `mnf` window. A consumer meaning "national
primetime package" should read `window_id`, not `is_primetime`.

## Fields that are null by necessity

Four spec fields have no free source. Each is emitted as `null` with an entry
in the row's `null_field_reasons`, rather than fabricated, defaulted or
dropped from the schema:

| Field | Reason emitted | What would fill it |
|---|---|---|
| `network` | `no_free_feed_carries_network` | a licensed schedule feed; `games.csv` has no `network` or `tv` column |
| `kickoff_local_time` | `venue_timezone_unavailable` | the `venue` collector's stadium→IANA table, at the price argued above |
| `announced_at` | `upstream_has_no_publication_instant` | any feed carrying a publication time. The spec forbids inferring it from the fetch time, and this one carries nothing else |
| `flex_decided_at` | `upstream_has_no_publication_instant` (or `no_change_observed` when nothing changed) | the same feed |
| `regional_coverage_pct` | `national_broadcast` **or** `no_free_regional_coverage_source` | a market-level coverage feed; the spec's own candidate is 506sports' scraped image maps, which is not a feed |

That last row is two facts, deliberately kept apart. The spec says
`regional_coverage_pct` is *"null for national"* games; here it is null for
**all** of them, and a consumer must be able to tell "null because there is
nothing to say" from "null because nobody publishes it". Collapsing them into
one value costs the reader the only thing the null could have told them.

`first_observed_at` carries the bound `announced_at` cannot — see below.

## The point-in-time guard

The spec's named failure mode is **retroactive certainty**, and it is an API
failure rather than a capture one: `/signals` serving the current state for a
past week gives a model foreknowledge it could never have had.

**`announced_at` is null, and `first_observed_at` is not a substitute wearing
its name.** `first_observed_at` is the capture instant of the first snapshot in
this collector's own lake carrying the game's *current* broadcast state. That
is an **upper bound** on the announcement: the change was announced at or
before the moment we first saw it.

The bound is sound in the direction the guard cares about. `first_observed_at
<= as_of` implies the state was certainly already public at `as_of`. It can
*withhold* a record announced before `as_of` that we did not observe until
after — a false negative, under-claiming knowledge — but it can never *admit*
one that was not yet announced. Under-claiming is the safe error for a
foreknowledge guard; over-claiming is the leak. Every row carries
`point_in_time_basis` (`announced` or `first_observed`) so the substitution is
never silent.

**`as_of` with a null timestamp excludes the row.** A record that passes a
point-in-time filter *because* its timestamp is null is the leak itself. The
predicate fails closed.

**The two-snapshot consistency check** is enforced at write time: a row
claiming `flex_status != original` must have at least two distinct observed
states behind it, differing in the dimension the status is about. A row that
fails it is refused — it never reaches the lake, it is recorded in
`coverage.missing` with `flex_history_unevidenced`, and
`broadcast_context_unevidenced_flex_claims` moves off zero. Each row also
publishes `observed_window_count`, so a consumer can apply the same check from
a single row.

### `games_in_window` and the partial-slate refusal

`games_in_window` counts games kicking off at the same **instant**, not games
sharing a `window_id`. The Divisional Round decides that: all four games carry
`window_id: playoff` and each is the only football on television when it kicks
off, so counting by window would report 4 for every one of them and
`is_standalone: false` for four standalone games.

A week is published only if its slate can be shown complete. It cannot be if
**any** game in it lacks a kickoff instant (that game could belong to any slot
in the week) or if a regular-season week lists fewer than 13 games (six byes is
the league maximum, so a shorter week means the document was truncated —
which `stream_csv_dicts` cannot detect on an uncompressed body). For every game
in such a week, `games_in_window`, `is_standalone` and `distribution` are null
with `incomplete_slate`, and `broadcast_context_incomplete_slate_weeks`
reports how many. **An undercount is the dangerous direction**: it
manufactures standalone primetime games that do not exist.

## Spec deviations

Four, all deliberate.

### 1. `as_of` is an optional filter, not a required parameter

The spec says *"require an `as_of` parameter on historical queries"*. Here it
is declared in `SUPPORTED_FILTERS` and applied in `signal_matches`, but a
query without it still returns the current state. Three reasons:

1. **The five-route contract is fleet-wide.** `scripts/smoke-test.sh` asserts
   a bare `GET /signals` returns 200 with an `envelopes` array for *every*
   registered collector, and "a generator that can consume one collector can
   consume all of them" is the whole extensibility mechanism.
2. **There is no hook to require it from.** The shared router hands a
   collector one per-row boolean predicate, so a "this parameter is required"
   error can only be raised from inside a row loop — which means the same
   query answers 200 against an empty cache and 422 against a populated one. A
   guard whose firing depends on cache state is not a guard.
3. **The leak is narrower on this API than the spec assumes.** `/signals`
   serves `CaptureState`, which holds exactly one capture, and the router
   drops any envelope whose scope does not match a requested `season`/`week`.
   Asking this collector for a past week returns *nothing*, not today's state
   wearing that week's label. The reachable residual is
   `POST /refresh {"week": N}`, which re-scopes the cache to week N using
   today's upstream — and `as_of` is exactly what closes it.

The lake, which is what a historical consumer actually reads, is append-only
and carries every snapshot's own `captured_at`, so point-in-time
reconstruction there is unaffected either way.

### 2. The two-snapshot check is generalised to `time_changed`

The spec's check is *"at least two distinct snapshots with differing
`window_id`"*. Applied literally it rejects every legitimate `time_changed`,
which by construction has **one** `window_id` and two different kickoff
instants. So the implemented rule is: a change requires two observed states
differing in the dimension the claimed status is about — `window_id` for
`flexed_in`/`flexed_out`, the whole `(window_id, kickoff_at)` state for
`time_changed`. Strictly stronger than the spec for the first two statuses and
satisfiable for the third.

### 3. `weeknight_special` is added to the `window_id` enum

The spec's enum has no bucket for a standalone game on a Tuesday, Wednesday or
non-holiday Friday. The real feed has three in two seasons: the 2025 opener
(Friday 20:00, São Paulo), the 2026 opener (Wednesday 20:20) and a 2026
Thanksgiving-eve Wednesday game. Leaving them unassigned would make each a
**permanent** coverage miss and — because an unslotted game could belong to
any slot — would null `games_in_window` for every other game in the same week.
One added enum value is a far smaller lie.

### 4. `distribution` is derived from slot structure, and `streaming_exclusive` is never emitted

With no `network` column, `distribution` is derived from reach: a game that is
the only one kicking off at its instant reaches every market (`national`); one
sharing its instant is regionally split (`regional`). **`national` here is a
claim about reach, not about carriage** — nothing free distinguishes a Prime
or Netflix exclusive from a broadcast one, so `streaming_exclusive` stays in
the schema (the spec defines it) and is never produced. A licensed feed would
fill it without a shape change.

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health`
and `/metrics` requires `Authorization: Bearer <token>`.

**No routes beyond the standard five**, per the spec. `as_of` is a filter for
that reason, not a `/history` route.

`/signals` filters: `game_id`, `window_id`, `flex_status`, `as_of` (plus the
universal `season`, `week`, `signal_type`). `as_of` must be an RFC 3339
instant with an explicit offset; a naive one is 422, because "some timezone
the caller forgot to state" would move the point-in-time boundary by hours for
exactly the rows near it.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Cost

`CAPTURE_ENABLED=true`, and it is the deliberate exception to the fleet
default. One pass reads **509 KB**. Every collector shipping `false` does so
because its upstream is 5–44 MiB — `officiating` reads 19.9 MiB — so this is
three orders of magnitude cheaper, and the feed serves an ETag and answers
`If-None-Match` with a 304 carrying zero bytes, so the steady state is one
round trip.

The decisive argument is not the size, though: **the flex history only exists
if the loop runs.** `flex_status`, `previous_window_id` and
`first_observed_at` are derived from this collector's own prior snapshots, so
with the loop off every game reads `original` forever, `observed_window_count`
is permanently 1, the two-snapshot consistency check can never accumulate
evidence, and `as_of` degenerates to a filter against a single capture
instant. That is not a degraded collector; it is the collector's product
switched off.

Two honest costs of `true`:

- The ETag store is in memory, so **every pod restart pays one full download**
  — ~0.5 MB per CI run, against `raw.githubusercontent.com`.
- On an ephemeral cluster the history still does not accumulate, because the
  lake is torn down with it. The flag's value is entirely in a long-lived
  environment.

The unchanged-snapshot digest gate is what keeps the accumulated history
affordable to read back: a snapshot is appended only when the published rows
actually change, so a season's partition holds the number of times the
broadcast schedule moved, not the number of times we looked. `read_history`
still caps at the newest 64 snapshots and says so in `errors` if it hits it.

**Known gap, inherited:** the ETag store is keyed by URL and this URL does not
vary by season or week, so `POST /refresh {"season": 2025}` after a 2026
capture can 304 into a no-op that still returns 202. Confirm a backfill by
reading `/signals`, not by the 202. See `docs/collectors.md`.

## Tests

```bash
cd services/broadcast-context
uv run pytest -v
```

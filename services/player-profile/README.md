# player-profile

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8019` |
| Gateway path | `/collectors/player-profile` |
| Cadence class | `static reference` |
| Signal types | `player_biographical`, `player_draft_capital`, `player_athleticism`, `player_career_load` |
| Scope-aware | yes — narrows to `roster-scope`'s membership list before fetching |
| Status | **Live**, deployed with `CAPTURE_ENABLED=false` (see *Cost*) |

## What it captures

"Who is this player, structurally" — the slow-moving attributes a projection
model uses as *features* rather than as weekly evidence: exact age at a given
date, draft pedigree, athletic testing, and how many career snaps the body has
already absorbed. No other collector carries a birth date, a draft position or a
combine measurement, and no other collector can say that a running back is on the
far side of his position's age curve while a receiver of identical age is not.

Three upstreams, all nflverse, joined forward through `player-identity`:

| Feed | Supplies | Size, measured 2026-07-31 |
|---|---|---|
| `players.csv` | birth date, position, height, weight, college, experience, the draft block | 7.32 MB, 25,029 rows |
| `combine.csv` | forty, bench, vertical, broad jump, three-cone, shuttle | 0.89 MB, 8,968 rows |
| `snap_counts_<season>.csv` | career offensive snaps, summed per season | 2.40 MB each, ~15 files |

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health` and
`/metrics` requires `Authorization: Bearer <token>`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

Plus one:

```
GET /signals/age-curve?position=RB&season=2026
```

The position-relative age distribution `position_age_percentile` was taken
against, **and** the `position_age_curve_stage` boundaries — because either
alone is unreproducible. It is served from the in-memory capture, not the lake,
so it always describes the same rows `/signals` is serving.

`/signals` filters: `season`, `week`, `signal_type` (universal), plus
`player_id`, `position` and `position_age_curve_stage`.

## Cost, and why `CAPTURE_ENABLED=false`

Per pass on a **cold process**: 7.32 + 0.89 + ~36 MB ≈ **44 MB**, of which the
career snap sweep is fifteen per-season files.

Steady state is far cheaper. Every one of those URLs answers `If-None-Match`
with a `304` carrying zero bytes — verified against the live assets — and a
completed season's snap file never changes again, so an established process pays
only for `players.csv` republications plus one current-season snap file.

But "steady state" is *per process*, and the ETag store is in memory. A Kind
cluster rebuilt on every CI run, and every pod restart, pays the full ~44 MB
again. That is roughly nine times `player-identity`'s ~5 MB upstream, and
`player-identity` ships `false` for exactly this reason. Flip it to `true` only
on a long-lived cluster.

`smoke.sh` therefore never posts `/refresh`: a dispatched refresh reaches the
upstream regardless of the flag.

## The staleness guard

The spec's named failure mode is a listed weight or position that silently goes
stale — a well-formed record carrying an old value, placing the player on the
wrong age curve, with nothing erroring. The guard is per-field:

* `position_last_changed_at` / `weight_lbs_last_changed_at` — when this
  collector first observed the value it is publishing now. `null` until a change
  has actually been observed, because stamping `now` on a first sighting would
  claim a change nobody saw.
* `position_last_confirmed_at` / `weight_lbs_last_confirmed_at` — when an
  adapter last **re-asserted** the value from a freshly served document.
* `position_stale` — true when in season and the confirmation is older than 45
  days.

**Conditional GET is load-bearing for this, not just for cost.**
`last_confirmed_at` advances only on a pass that actually re-read the document;
a `304` means nobody republished the value, so nothing was re-confirmed. Remove
the conditional request and every pass looks like a re-confirmation, the measured
age never exceeds one capture interval, and the assertion can never fire — the
guard would still be there, still green, and mean nothing.

`player_profile_stale_position_players` is the alertable series.
`collector_coverage_ratio` cannot see this failure at all: a player on a
two-month-old position still produces a complete, fully-covered record.

## Known gaps

* **`breakout_age_years` is null on every row.** The spec defines it as "age at
  first season crossing the position's breakout threshold", and neither half is
  available here — no upstream this collector reads carries per-season
  production, and the spec never defines the threshold. Emitting a number
  computed from a threshold this collector invented would be exactly the
  fabrication the spec's adapter notes forbid. The key is present and null so a
  consumer can tell "not supplied" from "absent field". Sourcing it needs an
  edge to `player-stats` or `usage-share` plus a defined threshold.
* **`career_offensive_snaps` is a floor for pre-2012 careers.** nflverse
  publishes snap counts from 2012 only, so a player who was already in the
  league reports `career_snaps_complete: false` rather than a total that is
  quietly short. The flag is `false` in three cases and only ever `true` on
  evidence: a rookie season before the window, a season file this pass could not
  read, **and an unknown rookie season** — the last one because a null is not
  evidence that a career fits inside the window, and treating it as one
  published a floor labelled complete.
* **A birth date after the capture's `as_of_date` yields a null `age_years`**,
  not a negative one, and files `birth_date_in_future` in `errors`. The null is
  indistinguishable in the row from the spec's permitted "no adapter could
  supply a birth date"; the error entry is what separates them.
* **`measurement_source_id` is null for ~9% of players.** `combine.csv` carries
  no GSIS id, so the join hops gsis → pfr → combine; 122 of 1,326 recent players
  (overwhelmingly undrafted rookies) have no `pfr_id` and therefore no
  measurements and no snap total. That is the spec's explicitly-optional case,
  not a coverage miss.
* **Both percentiles are `null` below `MIN_COHORT` (5).** The reference
  population is the pass's own captured players, so `(position,
  experience_seasons)` cohorts are small by construction and the snap-load
  percentile is null for a good share of veterans. A null is better than a
  percentile computed from three players, which a consumer cannot detect.
* **`_WriteObserver` is duplicated from `venue`.** Two copies is the point at
  which it should become a return value from
  `collector_core.publish.publish_capture`; not done here because it changes a
  signature nine collectors call.

## Tests

```bash
cd services/player-profile
uv run pytest -v
```

`tests/test_scope_narrowing.py` is the behavioural half of `scope_aware: true` —
the repo-root AST gate proves `ScopeClient` is imported, not that a scope failure
makes zero upstream calls.

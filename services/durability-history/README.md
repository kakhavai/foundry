# durability-history

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8020` |
| Gateway path | `/collectors/durability-history` |
| Cadence class | `seasonal` |
| Signal types | `player_durability_profile`, `player_injury_history`, `player_return_trajectory` |
| Scope-aware | yes — narrows to `roster-scope`'s membership list before any fetch |
| Status | **Live**, deployed with `CAPTURE_ENABLED=false` (see Cost) |

## What it captures

What happens to a player's body and production over the weeks *after* an injury.
`injury-report` says "questionable"; this collector says what questionable has
historically meant for this specific player — that he has strained the same
hamstring three times in two seasons, that his soft-tissue recurrences run about
eighteen days apart, and that his snap share sits near 60% of baseline for two
games after he returns.

**The work is event reconstruction, not row copying.** The upstream publishes one
line per player per team-week saying "Hamstring / Out". `events.py` collapses runs
of those into events with an onset, a resolution and a `days_to_return`, walking
the team's ordered completed-game list so a bye week cannot split one absence into
two. `derive.py` turns the events into rates — and declines to, below the sample
floor.

Two bounds on that, both of which exist because the alternative publishes a
plausible number rather than an obviously broken one:

* **A run stops at the season boundary, and so does its resolution.** An
  offseason is not a bye. Without the bound, a hamstring designated in the last
  week of one season and Week 1 of the next collapses into one event with a
  `days_to_return` of 339 — mostly offseason — which lands in
  `median_days_to_return_by_body_part` and in `/signals/return-profile`, and which
  hides a decision the recurrence rule should have made (split correctly, the
  second event is >90 days out and correctly novel). An event nobody returned
  from before the season ended is *unresolved*: the games stopped, so we do not
  know.
* **A played-through event gets no `days_to_return`.** He never left, so there is
  nothing to return from, and handing it the next game's date manufactures a
  return time out of the ordinary weekly cadence. Three played-through
  Questionable hamstrings would otherwise publish three 9-day "returns", cross
  `MIN_SAMPLE_EVENTS`, and unlock every derived rate for a player who has never
  been unavailable — while `career_games_missed_injury` correctly read 0. The
  events are still published; only the return time is withheld, which is exactly
  what keeps `sample_size_events` honest.

## The named failure mode, and the guard

> Games missed for non-injury reasons — a suspension, a personal-leave absence, a
> healthy scratch, or a late-season rest week for a team already seeded — get
> folded into `career_games_missed_injury`, and the player acquires a durability
> problem they do not have.

It looks entirely plausible: the availability rate is a believable number, just
wrong, and it biases every downstream projection for a player who has never been
hurt. `collector_coverage_ratio` cannot see it at all — the record is complete,
well-formed and fully covered.

The guard, both halves:

1. **`absence_reason` is sourced from the designation, never inferred from the
   absence.** `events.classify_absence` reads the injury report's own cause text
   and nothing else. A game with no designation is `undesignated` and is **never**
   counted as an injury. "Absent, therefore injured" is exactly the inference this
   forbids. The non-injury phrases are tested *before* any body-part token,
   because nflverse really publishes
   `Ankle [Not Injury Related - Personal, Thursday Only]`.
2. **The assertion.** Injury-attributed missed games never exceed the games that
   carried a designation. It holds by construction, which is why it is checked
   anyway: it is the property that breaks first if anybody ever makes absence
   imply injury. A violation is a priority error on every envelope plus
   `durability_history_attribution_violations`.

Both sides are **published**: `games_missed_by_reason` and `designated_games` on
every profile row, and a per-game `absences` array with a required
`absence_reason` on every injury-history row. A consumer can run the assertion
itself.

The deliberate consequence: an injury a club never reported is under-counted.
Under-counting biases toward "this player is fine", which is the safe direction.

## The recurrence rule — documented, versioned, and emitted

Two events at the same `injury_site` are linked when the later one's `onset_date`
falls within `RECURRENCE_WINDOW_DAYS` (90) of the earlier one's **return**
(`resolved_date`, falling back to `onset_date` while unresolved). Anchored on the
return because a re-aggravation is measured from when the tissue was last asked to
work again.

The rule and its version are emitted in `upstream.adapter`, **and the label
describes the rule the code actually runs**:

```
nflverse-injury-tables;recurrence=v1:same_injury_site_within_90d_of_return
```

Per the spec, and not decoration: without it, widening the window rewrites
`is_recurrence_of` on every historical event at once, and a consumer diffing two
lake objects sees every player's history change simultaneously with nothing in
either object to say a rule changed rather than a league of hamstrings.

**`injury_site`, not `body_part` — a deliberate refinement of the spec.** The
spec's `body_part` enum is ten values wide and has no member for a calf, a quad or
an oblique, so all three collapse to `other`. Keying recurrence on `body_part`
would link a Week 3 calf strain to a Week 8 quad strain as one re-aggravated
tissue — a fabricated recurrence, the same class of error as a fabricated absence.
`injury_site` is the finer normalized token and is **published on every event**, so
`is_recurrence_of` stays reproducible from the row. `body_part` is still emitted
exactly as the spec defines it.

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health` and
`/metrics` requires `Authorization: Bearer <token>`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

Plus one:

```
GET /signals/return-profile?player_id=fdy-a1b2&body_part=hamstring
```

The conditional return **distribution** for one player and one body part, because
a point estimate hides the variance the generator needs mid-recovery. It publishes
the player's own resolved returns, the captured population's returns at the same
body part to read them against, the count of the player's *unresolved* events
(dropping those would make a player who has never returned look like one with no
history), and `min_sample_events` so a null aggregate on the `/signals` row is
distinguishable from a bug. An unknown `body_part` is **422**, never an empty
distribution.

## Upstreams and cost

Five nflverse feeds, all read through `stream_csv_dicts` with conditional GET on,
all filtered as they parse. Measured 2026-08-01:

| Upstream | Size | Shape |
|---|---|---|
| `games.csv` | 2.17 MB | one file, 1999-present |
| `players.csv` | 7.32 MB | one file |
| `injuries_<n>.csv` | ~0.75 MB | one per season |
| `snap_counts_<n>.csv` | 2.40 MB | one per season |
| `stats_player_week_<n>.csv` | 8.28 MB | one per season |

At the default three-season window: **43.8 MB on a cold process, ~0 after**. All
five carry an `ETag` and answer `If-None-Match` with a `304` carrying no body
(verified against the live endpoints). A season that has ended is immutable, so
its three per-season files 304 forever.

`CAPTURE_ENABLED=false`, deliberately: the Kind cluster is rebuilt on every CI run
and every pod restart re-captures, which would put 43.8 MB of third-party traffic
on every PR. The cold cost is the whole problem; the steady state is free. Flip it
to `true` on a long-lived cluster, where one cold download buys a season of 304s.

Two narrowing decisions do the rest of the cost work, and both are *also* the
fail-closed path:

* **The scope and the identity seam are resolved before the first byte.** No
  scope means zero upstream calls.
* **Zero resolved rows means zero per-season fetches.** With nothing to filter
  for, the three sweeps would download ~34 MB and keep none of it — and a total
  `player-identity` outage is exactly when that happens.

`DURABILITY_HISTORY_SEASONS` is the cost dial: each extra season is ~11.4 MB cold.

## `absence_reason` does NOT match `injury-report`'s enum

Both collectors publish a field called `absence_reason`; they are different
vocabularies over different feeds (`injury-report` reads `INJURY_REPORT_URL`,
this reads the nflverse `injuries_{season}.csv` archive). A generator joining a
current-week reason to a historical one **must translate**:

| durability-history | injury-report | relationship |
|---|---|---|
| `injury` / `illness` / `personal` / `rest` | same | identical |
| `discipline` | `suspension` | **not** identical — `discipline` also covers a coach's decision / healthy scratch, which `injury-report` has no member for |
| `non_injury_other` | `unspecified` | a designation naming a non-injury cause this collector cannot classify further |
| `undesignated` | `unspecified` | **no designation existed.** One value upstream; split here because "the report said nothing" is what the named failure mode's guard turns on |

Reconciling the enums outright was considered and not done: renaming
`discipline` to `suspension` would mis-label a healthy scratch, and collapsing the
last two would delete the distinction the guard exists for. The table is
maintained in `contracts/collector-registry.yaml` beside this collector's
`depends_on`, which is where the generator reads the fleet's inventory, and
`tests/test_helm_values.py` fails if a published enum value is missing from it.

## Known gaps, stated rather than papered over

* **"Career" is a bounded window.** The spec says career-to-date; nflverse
  publishes injuries back to 2009, and a real career sweep is ~205 MB per cold
  process. Every row therefore carries `observation_window_first_season` — the
  first season of the WINDOW, not of the player's own career — and
  `career_history_complete`, and the window is in every envelope's `scope`. A
  truncated total labelled complete is a well-formed number that is silently
  wrong — the same call `player-profile`'s `career_snaps_complete` makes.

  **The per-season memos carry the scope keep-set they were built with**, and
  that is load-bearing rather than bookkeeping: prior-season files 304 forever
  and `roster-scope` membership changes weekly, so a season-keyed memo would
  serve a player who entered the scope after process start a history built
  without him — under `career_history_complete: true`. `player-profile` does not
  have this problem because it memoises the unfiltered table and narrows
  afterwards; pushing the filter into the parse is what creates it, and is also
  what keeps the 8.28 MB weekly-stats file from materialising ~4,600 unwanted
  players. See `tests/test_memo_scope_interaction.py`.
* **A mid-season trade attributes the whole season to the club the player spent
  longer at.** That under-counts tenure games at the other club, which biases
  `availability_rate` upward — toward "this player is fine". Splitting a season
  across two clubs is a genuine improvement, left undone rather than approximated.
* **A season in which nothing was observed contributes no games.** A player on a
  roster all year who neither took a snap nor made an injury report is invisible
  to every feed here. Inventing games he could have played would invent absences
  to explain.
* **`age_adjusted_availability_rate` ranks against the pass's own captured
  population**, not all of history — the same call `player-profile`'s percentiles
  make. It is null below `MIN_AGE_COHORT` same-position players inside the age
  band.

## Tests

```bash
cd services/durability-history
uv run pytest -v
```

`tests/test_absence_guard.py` is the one to read first — it is the named failure
mode, and the fixture's Charlie exists solely for it: he misses four of six games
in the scoped season and **not one of them is an injury**. A collector that
inferred injury from absence gives him `availability_rate: 0.78` instead of
`1.00`, and every other assertion about him still passes.

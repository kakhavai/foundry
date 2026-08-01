# coaching-scheme

A Foundry signal collector. See [`docs/collectors.md`](../../docs/collectors.md)
for the authoring guide.

| | |
|---|---|
| Port | `8023` |
| Gateway path | `/collectors/coaching-scheme` |
| Cadence class | `seasonal` |
| Signal types | `staff_assignment`, `team_scheme_profile` |
| Capture loop | **disabled** (`CAPTURE_ENABLED=false`) — see [Cost](#cost) |

## What it captures

Why a player's own history stops predicting them. When a team's play-calling
changes hands, the offence's pass rate over expectation can move ten points in
a week, and every receiver's target share is suddenly drawn from a different
distribution than the one their prior nine games describe. No player-level
collector can see that; this one names the regime, dates it, and quantifies
what changed on either side of the boundary.

Two signal types. `staff_assignment` is the **revision timeline** — who ran a
team, from which week to which. `team_scheme_profile` is that revision's
**scheme rates**: pass rate in neutral script, PROE, pace, no-huddle, shotgun,
play action, pre-snap motion, personnel groupings and fourth-down aggression,
computed over *that revision's weeks only*.

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health`
and `/metrics` requires `Authorization: Bearer <token>`.

Plus one:

**`GET /teams/{team_id}/revisions?season=`** — one team's ordered
staff-revision timeline, each revision carrying its scheme profile inline. The
join key (`revision_id`) is this collector's own invention, so a consumer
reconstructing this from `/signals` would be re-implementing the route. `404`
for a well-formed team with no timeline, `422` for a malformed team id or
season.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Upstreams

Four feeds. Sizes are what goes over the wire, measured live on 2026-08-01.

| feed | wire | owns | its failure |
|---|---|---|---|
| `games.csv` (nfldata) | **0.49 MiB** | the revision timeline | **fatal** |
| `play_by_play_<season>.csv.gz` | **18.22 MiB** | nine rate fields, the changepoint series | degraded |
| `ftn_charting_<season>.csv` | **7.75 MiB** | play action, pre-snap motion | field-level |
| `pbp_participation_<season>.csv` | **46.82 MiB** | `personnel_rates` | field-level |
| | **73.28 MiB** total per changed pass | | |

Only the schedule feed ends a pass. Losing play-by-play publishes
`staff_assignment` and a `present: 0` profile envelope; losing either charting
feed nulls the fields it owns and publishes everything else. That last split
matters most for the 46.82 MiB feed, which buys exactly one field of fourteen.

All four use conditional GET. A `304` is a **successful** capture. If every
feed 304s the pass is unchanged; if any one changed, the others are re-read
unconditionally — see `capture.py` for why all-or-nothing would discard
exactly the off-cycle coaching change this collector exists to catch.

### CSV, not parquet — decided once, for `defense-vs-position` too

nflverse publishes parquet variants roughly 10x smaller than the plain CSVs,
which would take a pass from 73.28 MiB to ~23.8 MiB. **Rejected**, on four
grounds, the first of which is decisive:

1. **The parquet is *larger* than the gzipped CSV on the biggest unavoidable
   feed.** `play_by_play_2025.parquet` is 19.40 MiB against
   `play_by_play_2025.csv.gz` at 18.22 MiB. Parquet's win exists only on the
   two feeds nflverse does not gzip.
2. **`pyarrow` is a 47.8 MiB wheel** (cp312 manylinux, >100 MiB installed) to
   save bandwidth on a loop that ships **disabled**. The image cost is paid on
   every build and pull; the bandwidth cost is currently paid never.
3. **Parquet's footer is at the end of the file**, so the body must be
   buffered before any row can be read — structurally reversing the streaming
   rule that fixed `roster-scope`'s OOMKill. Bounded here (4.52 MiB) but it is
   a second I/O idiom in a library built around one.
4. **CPU is not the constraint.** All four feeds stream in ~6.4s total through
   the shipped path, against a 300s `CAPTURE_DEADLINE_SECONDS`.

**`defense-vs-position` (collector 17) reads the same play-by-play document,
where parquet is the larger artifact, so the answer there is the same and for
a stronger reason.** Treat this as settled: take `.csv.gz` where nflverse
publishes one, plain CSV otherwise, and do not add `pyarrow` fleet-wide.
Revisit only for a collector that needs an nflverse asset which (a) has no
`.gz` variant, (b) exceeds ~40 MiB, **and** (c) ships `CAPTURE_ENABLED=true`.

## The two guards

Both are required by the spec's named failure mode, both are testable, and
each has two arms with a fixture per arm.

**Guard 1 — the window must not straddle** (`rates.py`). The structural half
comes first: `adapters/pbp.py` accumulates only per `(team, week)`, and a
revision's rates are a pure fold over the weeks inside it. There is no
season-to-date accumulator to attach to the wrong revision and no incremental
state to invalidate, which is what "recompute from scratch when a boundary is
inserted" asks for. The assertion is the second line of defence, and it
refuses on either (A) a sampled week outside the revision's span, or (B)
`games_sampled` exceeding the span — a team plays at most one game a week, so
arm B catches a duplicate fold that arm A cannot see. A refusal drops the row
and records the revision missing with a reason.

**Guard 2 — the changepoint the staff feed never saw** (`changepoint.py`).
A sustained shift of more than 8 PROE points holding 3+ weeks with no revision
boundary within 1 week of it. `detect` takes only a `(week, PROE)` series, so
it structurally cannot be biased toward confirming a revision; `explains` is a
separate function. Three arms: the mean shift, plus a run-length check on
**both** windows. The third arm is not symmetry for its own sake — without it
a lone outlier week is detected as a changepoint on the week *after* it,
because the outlier inflates the baseline and the return to normal reads as a
sustained drop. Measured and reproduced.

Per the spec, a flagged changepoint is **surfaced, not corrected**: both
sides' rates publish, carrying `changepoint_unexplained: true`, plus a
priority error on the envelope.

### Guard 2 is not optional on a live season

`games.csv`'s `away_coach`/`home_coach` columns **do** record mid-season head
coach changes — but only for seasons nfldata has back-filled. Measured against
the current file:

| season | mid-season head-coach changes in the feed |
|---|---|
| 2022 | 3 (CAR wk6, IND wk10, DEN wk17) |
| 2023 | 3 (LV wk9, CAR wk13, LAC wk16) |
| 2024 | **0** |
| 2025 | **0** |

2024 and 2025 each had at least three real ones — NYJ (Saleh, after wk5), NO
(Allen, wk9), CHI (Eberflus, wk13), TEN in 2025 (Callahan, wk6). The feed
carries the season-opening coach for all seventeen games of each.

So on a current season the staff feed's hypothesis is "nothing ever changed",
and the PROE changepoint test is the **only** detector there is.
`coaching_scheme_staff_revisions` sitting at exactly 32 is the signature of
that state, and it should be read next to
`coaching_scheme_unexplained_changepoints`.

## Play-caller identity, and the coverage tension

The spec requires `play_caller_id` non-null for `staff_assignment` coverage,
and separately states that this is the field with no reliable feed behind it.
Read literally, coverage is 0 forever.

**Resolution: follow the spec's coverage predicate exactly, over a curated
register with a mechanical expiry.** `coaching_scheme/play_callers.py` carries
the full argument; the short form:

- `play_caller_id`/`play_caller_role` come **only** from an entry in that
  register. Never inferred, never defaulted.
- Every entry cites a `source` and states the week range that evidence
  actually reaches. Past `asserted_through_week` it **expires** and the team
  returns to missing with `play_caller_assertion_expired`. That is the answer
  to "what happens in week 10 when the entry is three revisions stale": it
  stops applying, whether or not anyone remembers to revisit it.
- The register **ships empty**, because that is the honest state of this
  repository's knowledge. `staff_assignment` therefore reports
  `expected: 32, present: 0`, names every team in `coverage.missing`, and adds
  one priority error saying where the fix goes.

Two things keep that from being a dead red light. It is **movable** — the
remedy is a line in a committed file, not an upstream nobody controls — and it
is **isolated**: `team_scheme_profile` has its own coverage over a fully
sourceable universe and reaches 1.0 on a healthy pass. An operator sees a
curation gap next to a working rate pipeline.

**What was refused.** Defaulting `play_caller_id` to the head coach. The spec
itself says the play-caller is "frequently the head coach", which is exactly
what makes it poison: a play-calling handoff would show the id unchanged,
because the head coach did not change. The one signal this collector was built
for is the one such a default erases, and it erases it while looking correct.
Also refused: counting `role: unknown` with a null id as present, which would
report ratio 1.0 on a collector that knows nothing about its headline field.

## Null by necessity

Every one carries a machine-readable reason on the row, in `null_field_reason`.

| field | why |
|---|---|
| `offensive_coordinator_id` | No free feed names coordinators. The spec's own candidate is scraping Pro Football Reference staff pages. |
| `defensive_coordinator_id` | Same. |
| `change_reported_at` | No feed carries a staff-change publication instant. Inferring one from our own fetch time would state an observation as an announcement. |
| `fourth_down_go_rate_over_expected` | nflfastR publishes `wp`/`vegas_wp` but no win-probability-optimal fourth-down recommendation. A baseline invented here would look like the well-known public one and not be it. |
| `play_caller_id`, `play_caller_role` | Until curated. See above. |
| `personnel_rates` | Only when the participation feed was unavailable — otherwise populated. |
| `play_action_rate`, `pre_snap_motion_rate` | Only when the FTN charting feed was unavailable. |

## Spec deviations

Four, all disclosed rather than taken silently.

1. **`change_event` gains an `unclassified` member.** The feed states that the
   head coach changed and says nothing about *why* — dismissal, resignation,
   illness and interim promotion are indistinguishable in a name column.
   Picking one is fabrication; `none` is affirmatively false; a null loses the
   fact that a change happened at all, which is the one thing the feed does
   establish. `broadcast-context` added `weeknight_special` to its own enum on
   the same reasoning.

2. **Personnel, motion, play-action and no-huddle rates are populated.** The
   spec assumes an adapter may not have charted play-by-play behind it. It
   does: `pbp_participation` and `ftn_charting` are free and published. A
   deviation in the good direction, disclosed because the spec's coverage and
   failure discussion assumes those fields may be absent.

3. **The two signal types are keyed differently for coverage.**
   `staff_assignment` by team (the spec's own sentence); `team_scheme_profile`
   by `revision_id`, because a revision is what owes a profile and keying by
   team would let one revision of three stand in for the other two.

4. **The spec's field table is split across the two signal types.** Identity
   and staff fields go to `staff_assignment`, rate fields to
   `team_scheme_profile`, with `team_id`/`season`/`revision_id`/the effective
   weeks on both as the join.

## Known limitations

- **A play-calling handoff in weeks 1-2 is undetectable by guard 2** — there
  are fewer than three prior weeks to form a baseline. `detect` returns `None`
  there rather than firing off a one-week baseline.
- **`head_coach_id` is a name-derived slug**, not a resolved identity. A
  spelling change upstream mints a new id that reads as a coaching change;
  `head_coach_name` ships beside it so a consumer can tell those apart. Two
  coaches sharing a name would collide.
- **`personnel_rates` do not sum to 1.0.** 10 and 20 personnel are real and
  belong in none of the spec's five buckets. A residual is honest; closing the
  five would push those snaps into whichever bucket was written last.
- **`depends_on: schedule-context` is conceptual, not a call.** The season/week
  grid is read from `games.csv` directly, as `broadcast-context` does.

## Cost

`CAPTURE_ENABLED=false`, argued with the number in
`helm/values/coaching-scheme/values.yaml`. 73.28 MiB per changed pass is 3.7x
`officiating`'s 19.9 MiB, which already ships false, and the Kind cluster is
rebuilt on every CI run.

`broadcast-context` shipped `true` at 509 KB on the argument that **its** guard
is vacuous without a running loop — its flex history is derived from its own
prior lake snapshots. **This collector does not have that property.** Both
guards are computable from a single pass: the revision timeline from
`games.csv`'s per-week coach columns, the changepoint series from
play-by-play's per-week plays. Neither reads back a prior snapshot, so one
dispatched `POST /refresh` produces the same answer a season-long loop would.
The size argument is therefore uncontested.

The cost of that choice, stated: an observation-derived upper bound on
`change_reported_at` (broadcast-context's `first_observed_at` shape) would need
the loop, and is deliberately not built — precisely so the product stays
complete from one pass.

## Tests

```bash
cd services/coaching-scheme
uv run pytest -v
```

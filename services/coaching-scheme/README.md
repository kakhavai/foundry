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

### CSV, not parquet

**The fleet rule and its measurements now live in
[`docs/collectors.md`](../../docs/collectors.md#format-take-csvgz-where-it-exists-plain-csv-otherwise--not-parquet)**,
because it binds twenty-six collectors and nobody writing `betting-lines` will
read this file. The short version, and the correction that settles it: on
play-by-play the parquet (19.40 MiB) is **larger** than the `.csv.gz` (18.22
MiB), so the format that would justify a 47.8 MiB `pyarrow` wheel loses on the
one feed this collector cannot avoid.

One amendment specific to this collector, and it is the more useful framing:
**85% of the available saving is `pbp_participation` alone, and that feed buys
exactly one field of fourteen.** If the loop is ever enabled, the cheap fix is
dropping or de-cadencing that feed — no new dependency — not adding parquet
support. The question is field-per-megabyte, not format.

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

**Guard 2 — the changepoint the staff feed never saw. IT DOES NOT WORK, AND IT
SHIPS DISABLED.** This is the collector's biggest caveat and the first thing to
know about it.

The spec asks for a >8-point sustained PROE shift with no matching revision.
That was built, then measured against five live seasons (160 team-seasons,
2,718 team-weeks) with a **week-order-shuffled null** in which no changepoint
exists by construction:

| | real | shuffled null |
|---|---|---|
| spec's rule as written (>8 pts) | **65.0%** of team-seasons | **55.4%** |

Two thirds of the league flagged every year, at barely above the noise floor.
A threshold sweep separates them nowhere (8/10/12/14/16/18 points: real
65/39/19/7.5/3.8/1.9%, null 55/32/15/7.3/3.4/1.4%).

So the estimator was replaced with a pooled-variance max-t and the fixed
threshold with a **per-team permutation test**. That worked exactly as
designed — the false-positive rate lands on alpha at every level (5.0% at
α=0.05, 2.2% at 0.02, **1.1% at 0.01**) — and found nothing: **recall 0/13**
against known mid-season coaching changes, with the real firing rate sitting
on the null firing rate.

Then the ceiling on *any* detector: hand one the true changepoint week for
free, no search, no multiple-comparisons penalty.

    mean |shift| at a REAL coaching change     4.18 pts   mean |t| 1.05
    mean |shift| at a RANDOM week, same teams  3.92 pts   mean |t| 1.02
    within-team weekly PROE sd                 6.89 pts
    nominally significant (|t| >= 2)           2 of 13

A real coaching change is **indistinguishable from an arbitrary week**. NYJ
2024 moves +0.16, TEN 2025 −0.16, CHI 2024 +1.11; NO 2024 moves +2.98, the
wrong way. It is not the threshold, the estimator or the calibration — **the
signal is not in this statistic.**

`CHANGEPOINT_ENABLED = False`. Every row publishes `changepoint_unexplained:
null` with a machine-readable reason, never `false` — `false` would assert
"checked and clean", and a consumer filtering on it would treat unchecked rows
as verified. No priority error is raised and the gauge stays at 0. The
detector and its permutation harness are kept, not deleted, because the
calibration machinery is correct and a future statistic needs it to prove
itself; `test_flipping_the_flag_re_enables_the_wiring` keeps the disabled path
a switch rather than dead code.

**Follow-up:** weekly PROE may simply be the wrong series. `neutral_pass_rate`,
`personnel_rates` or `sec_per_play_neutral` may carry a sharper regime signal.
The oracle test above is the cheap way to check *before* building anything —
if the true-week effect does not clearly exceed the random-week effect, stop.
Those series were **not** tested here: swapping the spec's named statistic is a
design change, not a bug fix.

### What this costs, stated plainly

`adapters/games.py` measures that nfldata's coach columns carry **no**
mid-season change for 2024 or 2025 (2021-25: 2/3/3/0/0) despite several real
ones. Guard 2 was supposed to be the independent detector for exactly that
gap. With it disabled, **this collector has no working regime-change detector
on a current season.** `coaching_scheme_staff_revisions` at 32 means "the feed
says nothing changed", not "nothing changed", and nothing corroborates it.

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
- **Resolution is per revision, over the revision's own span** — never at the
  query week. Resolving once at the requested week and stamping the result on
  every revision would attach a week-10 play-caller, and their cited source,
  to a regime that ended in week 8. Requiring the assertion to cover the
  *whole* span is also what stops an assertion sourced through week 12 being
  stamped on a revision running to week 17 — the staleness bug, moved rather
  than fixed. An assertion covering only part of a revision is refused with
  `play_caller_changed_within_revision` rather than chosen between.
- The register **ships empty**, because that is the honest state of this
  repository's knowledge. Every row therefore carries `play_caller_id: null`
  with a reason, and one priority error names the file to curate.

**Coverage is not what reports this** — see deviation 5. Play-caller
completeness lives on `coaching_scheme_play_callers_identified` (0..32) and on
the per-row reason, so it moves independently of the schedule feed's health.
The curation gap is **movable** (a line in a committed file, not an upstream
nobody controls) and **separately visible**, which is what keeps it from
reading as an outage.

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

5. **`staff_assignment` coverage measures the grid clause only.** The spec's
   coverage sentence has two clauses — a revision covering the requested week
   AND a non-null play-caller. Scoring both makes the second **swallow** the
   first: with the register empty, `present` is 0 whether `games.csv` carried
   32 teams or 3, so the ratio can no longer report a truncated schedule feed
   at all. The clause that is fully sourceable becomes unobservable behind the
   clause that is not.

   So `present` counts teams with a covering revision — true, checkable, and
   otherwise unmeasured — and play-caller completeness is reported losslessly
   in three other places: `coaching_scheme_play_callers_identified` (0..32),
   the priority error naming the file to curate, and
   `play_caller_missing_reason` on every row.

   This is **not** the rejected option (c), which counted a null id as present
   and reported 1.0 while knowing nothing. Here `present` means "this team has
   a revision covering this week" — a narrower claim that is actually true —
   and the unsourced field keeps its own dial.

6. **Guard 2 ships disabled**, with `changepoint_unexplained: null` rather than
   the boolean the spec's field table implies. See "The two guards" for the
   five seasons of measurement; the spec asks for a detector that the
   available statistic cannot support.

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
- **`sec_per_play_neutral` is plausible but is NOT the published statistic.**
  Measured against live 2025 through the shipped adapter: league mean
  **32.37s**, median 32.58s, range 29.90 (NO) to 34.55 (BUF). That is ~3-4s
  above commonly published neutral-pace figures, and the inter-team spread is
  only **4.64s** where published spreads run wider — the signature of a noisy
  estimator regressing to the mean. The season yields just **13,945 clock
  samples** (~26 per team-week), because the drive-keyed reset discards the
  first snap of every drive. Treat it as an internally consistent pace proxy,
  not as a figure to compare against a public one.

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

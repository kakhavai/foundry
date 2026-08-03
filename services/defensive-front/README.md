# defensive-front

A Foundry signal collector. See [`docs/collectors.md`](../../docs/collectors.md)
for the authoring guide and
[`docs/architecture/phase-8-data-source-collectors.md`](../../docs/architecture/phase-8-data-source-collectors.md)
for the spec this implements — including the two disclosed deviations below.

| | |
|---|---|
| Port | `8026` |
| Gateway path | `/collectors/defensive-front` |
| Cadence class | `weekly` |
| Signal types | `defensive_front_strength` |
| Depends on | `player-identity` (for `key_absences` only) |
| Scope-aware | no |
| `CAPTURE_ENABLED` | **`false`** — see [Cost](#cost) |

## What it captures

How much disruption a defensive front generates *before* the offence's own
quality is factored in — the input to quarterback sack risk, pressure-driven
interception rate, and the yards a running back gains before he is touched.
One row per defence per pass, from four free nflverse feeds. It is the
deliberate mirror of the (unbuilt) `offensive-line` collector: the generator's
matchup feature is `front.<metric>_generated_adj − line.<metric>_allowed_adj`,
joined on `(season, week, team_id)` with no unit conversion, so the shared-stem
metrics here are emitted on the scale that field will have to match.

**This collector was on the "needs a paid charting provider" list and is
not.** Sixteen of the spec's eighteen fields come from feeds the fleet already
reads. The charting columns were verified populated against the live 2025
regular season before a line was written — see [Upstreams](#upstreams).

## Upstreams

Freshness re-checked live against the nflverse releases API for this build,
**before** size was considered: `player-contract` shipped against a `.csv.gz`
nflverse had stopped rebuilding four years earlier and published the 2022
offseason as current. Every format of every feed below shares a timestamp, so
that exception does not apply and the fleet-wide format rule does.

| feed | size | updated | fatal? | what its loss costs |
|---|---|---|---|---|
| `play_by_play_2025.csv.gz` | 18.22 MiB | 2026-02-12 | yes | everything |
| `pbp_participation_2025.csv` | 46.82 MiB | 2026-02-10 | yes | 7 of 16 fields, and the guard |
| `players.csv.gz` | 2.39 MiB | 2026-08-01 | no | `front_continuity_index`, `key_absences` |
| `injuries_2025.csv.gz` | 0.12 MiB | 2026-03-18 | no | `key_absences` |

**`injuries` is a fourth feed the source hunt did not list, added
deliberately.** `key_absences` is "front starters listed out or doubtful for
the upcoming week" — game status, not roster status. `players.csv`'s `status`
column (`ACT`/`RES`/`DEV`) says nothing about whether a healthy-rostered
starter is sitting Sunday, and reading it as an absence would populate the
field with a different quantity than the one the spec names. Being correct
costs 0.12 MiB of a ~67.6 MiB pass — 0.18%.

**Participation is fatal, which departs from `team-scheme`'s treatment of the
same feed.** There it bought one field of thirteen and was rightly degraded to
a null. Here it carries pressure rate, its adjusted counterpart, blitz rate,
pressure when blitzing, pressure-to-sack conversion, mean release time faced
and front continuity — plus the timing guard's whole independent variable. A
row with every pressure column null is a complete-looking row describing
nothing, which is worse than a `present: 0` envelope that says so.

**Format: no parquet.** `pbp_participation` has no `.csv.gz` and is 46.82 MiB
against a 4.52 MiB parquet, which meets two of `docs/collectors.md`'s three
conditions for revisiting the rule — and fails the third: `CAPTURE_ENABLED` is
`false`, so those 42 MiB are spent only on a hand-dispatched `POST /refresh`,
against a `pyarrow` wheel that is 47.8 MiB in **every** collector image.

### Populated rates, measured live on 22,002 charted pass-rush snaps

| column | populated | shape |
|---|---|---|
| `was_pressure` | 100% | `TRUE`/`FALSE` |
| `number_of_pass_rushers` | 100% | 0 on runs, 4 on a base rush, 5–6 on a blitz |
| `defense_players` | 100% | semicolon-joined GSIS ids |
| `time_to_throw` | 42.8% | dropbacks that were actually thrown |

`time_to_throw` being under half is correct rather than a gap: a sack, a
scramble and a throwaway have no release.

## Two deviations from the spec — disclosed, not worked around

### 1. `unit` is `overall` only

The spec declares `overall | interior | edge`, and its own adapter note says
the split "has to come from **alignment technique on the snap**, not from the
defender's listed position, or every 3-4 outside linebacker lands in the wrong
bucket". **No free source publishes alignment technique.** Participation gives
`defense_players` (ids) and `defense_positions` (roster-listed) — precisely the
basis the spec rules out. Synthesising the split would produce two populated,
plausible, wrong columns, which is worse than one honest one.

The schema's enum is narrowed to `["overall"]` with `additionalProperties:
false`, so a row claiming `interior` fails conformance rather than reaching the
lake.

The one place listed position *is* used is the coarse front-versus-secondary
cut for `front_continuity_index` (`position_group` `DL`/`LB` against `DB`).
The spec's objection does not generalise to it: a 3-4 outside linebacker is
listed `OLB`, which is in `LB`, which is in the front either way, and no listed
position puts a safety in the front or a nose tackle out of it.

### 2. `yards_before_contact_allowed_per_carry` and its `_adj` are null

Present-and-null with a machine-readable reason in `null_field_reason`, and
`"type": "null"` in the schema so a later "fill-in" fails conformance. Pro
Football Reference publishes yards before contact **season-level and on the
offence's side of the ball**, so it cannot be attributed to the opposing
defence, and nothing free publishes it per play. It is deliberately **not**
derived from anything: it measures tackling depth, and
`adjusted_line_yards_allowed` is a different quantity.

### ...and the coverage predicate moves with deviation 1

The spec's clause is 32 defences × three `unit` values (96 rows), "a team is
present only when `overall`, `interior` and `edge` are all populated". With
only `overall` sourceable that predicate is **0.0 forever** — and worse, a
ratio pinned at zero cannot report anything else either: a truncated upstream,
a dead join and a half-empty week all read identically. The clause would
swallow the metric it belongs to.

So `coverage.expected` is **32**, one per defence, declared independently of
any fetch, and a team is `present` when its row carries a pass-rush sample.
Both halves move together; see `capture.EXPECTED_FLOOR`.

## The timing-confound guard — the spec's named failure mode

> regress `pressure_rate_generated_adj` on `mean_time_to_throw_faced` across
> the 32 teams and require the residual slope to be statistically
> indistinguishable from zero; a non-zero slope means the adjustment model is
> missing the timing term entirely.

Implemented in [`timing.py`](defensive_front/timing.py), run on every pass over
exactly the rows about to be published, and **measured on the real 2025
regular season through the shipped code path**:

| | |
|---|---|
| slope | **−0.04940** pressure-rate per second |
| standard error | 0.10050 |
| t | **−0.4915** on 30 df (critical value 2.0423) |
| p | 0.6266 |
| 95% CI | **[−0.25464, +0.15585]** — contains zero |
| verdict | **PASSES** |

**Checked against a null before it was trusted.** Permuting
`mean_time_to_throw_faced` across the 32 teams 20,000 times fires the guard
**4.66%** of the time, against the 5.00% an α=0.05 test fires by construction.
It is calibrated — contrast `defense-vs-position`'s rank guard, whose null
fired 54%, and `coaching-scheme`'s changepoint detector, which fired on 65% of
teams against a 55% null and shipped disabled.

**It can fire.** Injecting a genuine confound into the published column
(`adj += k·(ttt − mean)`) leaves it passing at k=0.20 and fires it at k=0.30
and k=0.50. At df=30 the minimum detectable R² is **0.122**, so it catches a
confound explaining ~12% or more of the cross-team variance in the adjusted
rate — about 5.0 percentage points of pressure rate across the observed
0.2601 s spread in release timing, against a 13.40-point total spread. Both
arms are driven end to end in `tests/test_timing_guard.py`.

### Read a `false` with its power in mind — especially late in a season

**This is the guard's most important limitation and it belongs here, not in a
report.** The test's power is a function of the spread in
`mean_time_to_throw_faced` across the league, and a schedule averages that
spread away as a season runs. By week 18 of 2025 it was **0.2601 s across all
32 teams** — against a minimum detectable R² of 0.122. At that point even the
**unadjusted** pressure rate shows no relationship with release timing
(t −0.24), so the pass above is close to the strongest result the data can
produce whatever the adjustment does.

Consequences a consumer should act on:

* A `timing_confound_flagged: false` on a **late-season** row is weak evidence.
  It is not a certification that the opponent adjustment is clean against the
  spec's named failure mode.
* The guard is **most informative in weeks 4–6**, when schedule imbalance is
  largest and the regressor genuinely varies. That is when a flag means
  something and when a pass means something.
* Always read **`timing_guard_ran`** first. `false` there means the guard did
  not run at all — fewer than four comparable defences, or no spread in faced
  release time — and `timing_confound_flagged` is then `false` because nothing
  was tested, not because nothing was found.

The same caveat is carried on `timing_confound_flagged`'s description in
`contracts/signal-envelope/collectors/defensive-front.json`, so it reaches a
generator that never opens this file.

### Why the adjustment does not residualise on this variable

The obvious way to "add the timing term" is to regress the adjusted rate on
team-mean release time and publish the residual. **That would make this guard
structurally incapable of firing** — an OLS residual is orthogonal to its own
regressor by construction, so the slope would be exactly 0.0 on every pass, for
every dataset, including one where the confound is total. An unfailable guard
is worse than no guard.

The timing term is not missing; it arrives through the opponent yardstick.
`ratings.opponent_strengths` is fit on the opposing offence's own **pressure
rate allowed**, leave-one-out over that offence's *other* games — and an
offence's release timing drives what it allows. Measured **at the offence-game
level, which is where `opponent_strengths` actually estimates**, over the
joined play set it is actually fit on: `pressure_allowed ~ own
mean_time_to_throw` has slope **+0.0733**/s, **t +5.05 on 541 df**, r **+0.212**,
n **543**. Offences that hold the ball longer allow more pressure, so a
quick-release offence is rated as a strong line and a defence that faced it is
adjusted **up**.

An earlier revision of this section cited +0.106/s, t +2.10 at the
offence-*season* level. That number is real but is computed over *all* charted
snaps rather than the joined set the adjustment uses; on the joined set it is
+0.0904/s, **t +1.79 — not significant at 5%**. Same direction and magnitude,
but the significance claim did not survive, so the offence-game figure above
replaces it.

Because that estimate is per opposing offence-game rather than from the 32 team
means, the residual slope is a genuine test rather than an identity — and the
fixtures prove both halves separately:
`test_an_offense_mediated_timing_effect_is_ABSORBED` drives a confound through
the offence and the guard keeps passing to six times the real effect size;
`test_a_confound_the_adjustment_cannot_absorb_FIRES_the_guard` drives one the
yardstick cannot see and it fires.

## The opponent adjustment

`adjustment_method` is `opposing_offense_production_ratio_loo_v1`, published on
every row so a consumer reading an old lake object can tell which vintage it
holds.

Fit on the **opposing offence's own production**, never on a prior defensive
rating: a rating is already a function of the units faced, so adjusting by
ratings feeds the quantity back into its own estimate. Leave-one-out, so a
front that flattened an offence is not told that offence is weak partly
*because* of the flattening.

**Check the variance before believing an adjusted column.**
`defense-vs-position` published a column that was the league mean for all 32
teams while the raw value spanned 4×, because it adjusted a unit by its own
leave-one-out mean of the quantity being rated — and the mean of a unit's
leave-one-out means is exactly its full mean. Coverage stayed 1.0 throughout;
the only symptom was zero variance. Measured here on real 2025 data:

| column | min | max | population variance |
|---|---|---|---|
| `pressure_rate_generated` (raw) | 0.2153 | 0.3493 | 1.1555e-03 |
| `pressure_rate_generated_adj` | 0.2207 | 0.3655 | **1.2631e-03** |
| `sack_rate_generated_adj` | 0.0336 | 0.1051 | 2.4930e-04 |
| `adjusted_line_yards_allowed` | 2.5489 | 3.4005 | 3.8171e-02 |
| `opponent_pressure_strength_index` | 0.9356 | 1.0644 | 9.2950e-04 |

The adjustment slightly *increases* spread, which is what a real correction
does. `defensive_front_adjusted_variance` is a gauge for exactly this, and
`tests/test_ratings.py` asserts the mis-keying still collapses — so the
variance assertions prove something rather than decorate.

## Definitions that must not drift

* **Pressure is attributed to the rushing unit, not the play outcome.**
  `was_pressure` is charted at the snap, independently of `sack`, so hurries
  and knockdowns count even when the ball is out. Nothing in the adapter reads
  an outcome column, which is the structural version of the requirement.
* **The two feeds are joined as an intersection.** A play counts only when
  play-by-play calls it a regular-season dropback in the window AND
  participation charted a pass rush on it. **5.24%** of charted pass-rush snaps
  are penalty-nullified `no_play` rows, which can carry a pressure but never a
  sack; counting them would deflate `pressure_to_sack_rate` by that much while
  every field stayed populated and plausible.
* **`adjusted_line_yards_allowed` uses the Football Outsiders line-yards
  weighting** — 120% credit behind the line, full through 4 yards, half from 5
  to 10, none past 10 — in named constants in `ratings.py`. **`offensive-line`
  must import the identical weighting**: the spec says a divergence corrupts
  the head-to-head differential silently rather than failing.
* **`front_continuity_index`** is the share of the window's front-player snaps
  taken by the current front seven — the seven front defenders with the most
  snaps over the last **three** sampled weeks. Three, not one, and the reason
  is measured: a one-week window is destroyed by week-18 rest, which on real
  2025 data read Green Bay at 0.118, Buffalo at 0.278 and the Chargers at
  0.318, every one a rest-week artifact rather than a front that turned over.
  Three weeks moves those to 0.590, 0.589 and 0.659, cuts the cross-team
  variance 3.5× and lifts the league minimum from 0.118 to 0.469; a fourth week
  changes the minimum not at all. Denominator is actual front participations,
  not `7 × snaps` — nickel is the modern base defence and drops a linebacker.

## `key_absences` and `player-identity`

The only field that reaches another service. Front players listed **Out** or
**Doubtful** for week + 1, resolved through `POST /resolve/batch`.
`Questionable` is excluded — the spec says out or doubtful, and 1,281 of the
real 2025 feed's rows are questionable against 1,396 out.

Measured against the live 2025 injury report: **28–30 front players across
17–22 teams** in a typical week.

**It has never run against a real `player-identity`.** Every test here drives
a mocked service, and `helm/values` points `PLAYER_IDENTITY_URL` at
`player-identity:8002`, so the first real resolution will happen in-cluster.
An outage or a refusal is field-level — `key_absences` is empty and the pass
files an `identity_unavailable` or `identity_unresolved` coverage error — so
the failure mode is a quiet field with a loud reason rather than a broken
capture. Check those reasons on the first in-cluster pass.

**The 500-query hazard.** `player-identity`'s `build_query` raises a 422 for a
position outside `KNOWN_POSITIONS` and `resolve_queries` calls it *inside* the
loop over the batch, so **one unmapped code fails all 500 queries**, not one
row. nflverse publishes `SAF` for 345 players and `KNOWN_POSITIONS` carries
`S`, `FS` and `SS` but not `SAF`. Audited live for this build: **`SAF` is the
only unmapped code** in either `players.csv` (25 distinct codes) or
`injuries_2025.csv` (16, all known). `SENDABLE_POSITIONS` sanitises anyway —
this collector's front filter already excludes safeties, but that is a
consequence of another decision and would survive exactly until somebody
widened it. The real fix belongs in `player-identity`'s batch loop; like
`defense-vs-position`, this collector guards its own side and does not reach
into the shared library.

## Cost

**~67.6 MiB per changed pass**, dominated by participation (46.82 MiB, 69%).
Conditional GET on all four feeds; a `304` on every one of them is a
**successful** capture that downloads nothing.

**`CAPTURE_ENABLED=false`, and the argument is a number.** Verified live for
this build: `play_by_play_2026.csv.gz` **404s today**, and so does
`pbp_participation_2026.csv`. A running loop would therefore fetch nothing,
fail every pass, and pin `collector_coverage_ratio` at 0.0 for the entire
offseason — 67.6 MiB of 404s per interval buying zero data, in a cluster that
is recreated on every CI run and re-captures on every pod restart. Flip it to
`true` when the 2026 artifacts exist and the weekly cadence is worth 67.6 MiB;
until then a dispatched `POST /refresh` reaches the upstream regardless of the
flag, which is the supported way to backfill.

There is no `smoke.sh`: this collector adds no route beyond the standard five,
and the standard surface is asserted for every registered collector
automatically.

## Tests

```bash
cd services/defensive-front
uv run pytest -v
```

214 tests, 99% line and branch coverage. The four documents are built in the
real wire format by [`tests/season.py`](tests/season.py) — **read its docstring
before touching a fixture.** Its shape is what makes the suite capable of
failing: every pressure is `f(offence) + g(defence)`, because a fixture varying
by the offence alone has a *correct* adjustment remove 100% of the variance and
collapse to the league mean, which is bit-for-bit identical to the bug it is
supposed to catch. The schedule is five rounds of a seven-round circle rather
than a complete round robin, because a complete one gives every defence the
same opponent set and the timing guard would have no gradient to measure.

The only uncovered lines are the three underflow guards inside the incomplete
beta's continued fraction (`timing.py`), which are the published algorithm's
own defensive branches and are unreachable with well-formed inputs. Stated
rather than covered by a contrived test.

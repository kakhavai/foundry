# team-scheme

What an offense is actually doing right now, measured from play-by-play and
keyed to a **team-season**. See [`docs/collectors.md`](../../docs/collectors.md)
for the authoring guide and
[`docs/architecture/phase-8-data-source-collectors.md`](../../docs/architecture/phase-8-data-source-collectors.md)
(`#### team-scheme`) for the spec.

| | |
|---|---|
| Port | `8023` |
| Gateway path | `/collectors/team-scheme` |
| Cadence class | `seasonal` |
| Signal types | `team_scheme_profile` |
| Extra routes | none |
| `CAPTURE_ENABLED` | **`false`** — see *Why the loop ships disabled* |

## What it captures

One profile per `(team, season)`: how often an offense passes relative to
expectation, how fast it plays, what personnel it lines up in, how often it
motions or play-actions, and how aggressively it goes for it on fourth down.
Every one of those is measured from play-by-play, independently of who is
coaching.

| field | source |
|---|---|
| `neutral_pass_rate` | pbp, snaps in quarters 1–3 with win probability 0.20–0.80 |
| `pass_rate_over_expected` | pbp `pass_oe`, nflfastR's own model. **Percentage points** |
| `sec_per_play_neutral` | pbp inter-snap clock delta within a drive, bounded 1–60s |
| `no_huddle_rate`, `shotgun_rate` | pbp |
| `fourth_down_go_rate` | pbp, denominator is every fourth-down *decision* |
| `personnel_rates` | `pbp_participation` `offense_personnel` |
| `play_action_rate`, `pre_snap_motion_rate` | `ftn_charting` |
| `fourth_down_go_rate_over_expected` | **null by necessity** — see below |

## This collector was split out of `coaching-scheme`

It was originally specified as one collector carrying both these rates and a
**coaching-staff timeline**, with the rates keyed to a staff revision so that a
mid-season coordinator change produced two profiles rather than one blend.
That is the better product, and it is unavailable. Every free source was
enumerated during 8E:

| source | verdict |
|---|---|
| nflverse, all 25 data releases | no coaching feed exists |
| nfldata `games.csv` coach columns | correct through 2023, **wrong from 2024** — every row carries the opening-day coach, so 2024 NYJ shows Saleh for all 17 games though he was dismissed in week 5. Same for NO, CHI and TEN |
| ESPN core API, season-scoped coaches | returns **today's** staff for every season queried |
| Pro Football Reference | HTTP 403 to automated requests |

A staff timeline for a current season would therefore be **false, not merely
incomplete** — claiming one regime where there were two — and that false claim
propagates into any rate keyed to it, producing the spec's own named failure
mode through the *data* rather than through the code. Two workarounds (a
committed manual-override file; snapshotting a current-state source so our lake
accrues the transitions) were rejected on principle: both make this project the
permanent maintainer of another project's data quality.

So the staff fields, the revision timeline and the
`GET /teams/{team_id}/revisions` route are deferred to a **`coaching-staff`**
collector, marked paid-vendor-required in the phase doc.

**The consequence, stated rather than hidden:** a mid-season coordinator change
now produces a *blended* season profile. That is a true statement about the
season and a poor predictor of next week. It is the honest answer available,
and it is strictly better than a confident false attribution — but a consumer
modelling week-to-week regime change needs `coaching-staff` to exist first.

**Do not reintroduce revision-keyed rates against an unreliable staff feed.**
`tests/test_rates_window.py`'s last section fails if any staff-derived field or
identifier comes back.

## Why the loop ships disabled

`CAPTURE_ENABLED=false`. **73.28 MiB per changed pass** — the largest per-pass
footprint in the fleet, 3.7x `officiating`'s 19.9 MiB — against a Kind cluster
recreated on every CI run, where every pod restart re-captures.

`broadcast-context`'s *vacuous-without-the-loop* exception does not apply, and
that is the load-bearing check rather than the size. `broadcast-context` ships
`true` because its flex history is derived by comparing each pass against **its
own prior lake snapshots**: no loop, no history, product switched off. Nothing
here reads back a prior snapshot — every rate comes from one whole-season
document — so **one dispatched `POST /refresh` produces exactly the answer a
loop running since week 1 would.** The vacuity argument is unavailable and the
size argument is uncontested.

A dispatched `/refresh` reaches the upstreams regardless of the flag. There is
no `smoke.sh`, so nothing in CI dispatches one.

## Format: CSV, not parquet

Decided fleet-wide and recorded in
[`docs/collectors.md`](../../docs/collectors.md#format-take-csvgz-where-it-exists-plain-csv-otherwise--not-parquet).
The decisive fact is that on play-by-play — the feed this collector cannot
avoid — **the parquet (19.40 MiB) is larger than the gzipped CSV (18.22 MiB)**.
Parquet's win exists only on the two feeds nflverse does not gzip, and it does
not buy a 47.8 MiB `pyarrow` wheel in every collector image.

Worth restating the amendment, because it is what to do if the loop is ever
enabled: of the 49.5 MiB available saving, **42.3 MiB (85%) is
`pbp_participation` alone, and that feed buys exactly one field of thirteen**
(`personnel_rates`). De-cadencing or dropping it costs one field and no
dependency; adding `pyarrow` costs a fleet-wide wheel and keeps the feed.

## Honest nulls and unvalidated numbers

**`fourth_down_go_rate_over_expected` is null by necessity.** nflfastR
publishes `wp` and `vegas_wp` but no win-probability-optimal fourth-down
recommendation column — the public bots that produce one are separate projects
with their own models. A baseline invented here would look like the well-known
public one and not be it. The row carries the reason in `null_field_reason`.

**`sec_per_play_neutral` ships and is UNVALIDATED against any published
figure.** Run against live 2025 through the shipped adapter it gives a league
mean of **32.37s**, a median of 32.58s, a range of 29.90 (NO) to 34.55 (BUF) —
an inter-team spread of only **4.64s** — over **13,945 clock samples** (~26 per
team-week, because the drive-keyed reset discards the first snap of every
drive). That is plausible and internally consistent, and it is ~3–4s above
commonly published neutral-pace figures with a narrower spread than published
ones show — the signature of a noisy estimator regressing to the mean. Treat it
as a pace *proxy* until someone checks it against a known team-season figure.
It is the largest unverified numeric claim in this collector.

**`pass_rate_over_expected` is in percentage points**, not a share. Read as a
share it is off by 100x and every value still looks plausible. The contract
schema says so on the field.

**`no_huddle_rate` has a known upstream outlier.** Live 2025 through this
adapter publishes **0.6223 for WAS** against a league median of **0.0748** and
a second-highest of **0.2264** — eight times the median and 2.7x the next team.
A 62% no-huddle rate is not a thing an NFL offense does. The arithmetic here is
correct; nflfastR's `no_huddle` column for that team-season almost certainly is
not.

It is **published unchanged, and reported**. This collector has one source for
the field and no second opinion, so it cannot support the claim that the
upstream is wrong — but it can support "one team is unlike the other
thirty-one", which is a statement about the pass and is checkable. So the
envelope's `errors` carries a `rate_outside_the_shape_of_this_pass` entry
naming the team, the field, the value and the band, and
`team_scheme_rate_outliers` counts them. Nothing is dropped and coverage does
not move.

The rule asserts **no league-average prior**: a rate is reported when it sits
further outside the other teams' range than that range is wide. The only tuned
quantity is the multiplier, and it is 1.0. See the reasoning block above
`flag_dispersion_outliers` in `rates.py`, including why a *refusing* bound was
rejected — a threshold tuned to today's distribution becomes a filter on
tomorrow's signal, which is the changepoint detector's pathology in a different
costume.

Note this is a different problem from the `no_huddle_rate` **denominator**
question (every offensive snap rather than every pre-snap opportunity), which
is a definitional choice. The WAS value survives any denominator.

## What is deliberately not built

**The changepoint detector.** The original spec required a test on each team's
weekly PROE series to catch an unannounced play-calling handoff, firing on a
sustained shift beyond roughly eight points. It was built, measured across five
seasons, and **does not work** — and the reason is not the threshold. An oracle
test (the true changepoint week supplied for free, no search and no
multiple-comparisons penalty) gives a mean absolute shift of **4.83 points at a
real head-coach change against 4.01 at a random week**, with a within-team
weekly standard deviation of **6.89** (p = 0.18, n = 12, 2021–2025). The
defensible claim is that any regime effect on weekly team PROE is smaller than
roughly six to eight points and not separable at this sample size. Of six
candidate series tested with the same oracle, **shotgun rate is the only one
that separates** (1.74 vs 1.15, p = 0.038 — suggestive, not established after
correcting for six tests), and `sec_per_play_neutral` performs *worse* than
random. The phase doc carries the full result. **Do not re-run that analysis
and do not rebuild the detector from PROE.**

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.

`GET /signals` accepts `season`, `week`, `signal_type` and `team_id`. **`week`
scopes the capture, not the rates**: a profile is folded over every week of the
team's season, so `?week=9` means "the capture taken at week 9", not "what this
team did in week 9".

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Tests

```bash
cd services/team-scheme
uv run pytest -v
```

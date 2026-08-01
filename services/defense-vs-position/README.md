# defense-vs-position

A Foundry signal collector. See [`docs/collectors.md`](../../docs/collectors.md)
for the authoring guide and
[`docs/architecture/phase-8-data-source-collectors.md`](../../docs/architecture/phase-8-data-source-collectors.md)
for the spec.

| | |
|---|---|
| Port | `8025` |
| Gateway path | `/collectors/defense-vs-position` |
| Cadence class | `weekly` (1-day base interval) |
| Signal types | `defense_positional_allowance` |
| Depends on | `player-identity` |
| Scope-aware | no |
| `CAPTURE_ENABLED` | **`false`** — see [Cost](#cost) |

## What it captures

What a given defense concedes to the **position slot** a projected player
occupies, rather than to the offense as a whole. A defense can be top-five
against one position and bottom-five against another, and a player-level
projection needs the second number rather than the team's overall
yards-allowed rank.

It is the only collector that decomposes allowance into the fantasy-scoring
components — targets, receptions, yards, YAC, touchdowns — so the generator can
distinguish a defense that concedes *volume* from one that concedes
*efficiency*. Every value is published both raw and opponent-adjusted, because
a raw rating in Week 4 is largely a description of which offenses the defense
happened to draw.

**576 rows a pass**: 32 defenses × 6 positions × 1 alignment × 3 scoring
formats.

## Upstreams

| Feed | Size | Buys |
|---|---|---|
| `pbp/play_by_play_{season}.csv.gz` | **18.22 MiB** | every opportunity, its yardage and its game context |
| `players/players.csv.gz` | **2.39 MiB** | `gsis_id → position`, and the fields `player-identity` scores a resolve query on |
| `player-identity` `POST /resolve/batch` | — | the canonical id every opportunity must resolve to before it is attributed |

**20.61 MiB per changed pass.** Both CSVs use conditional GET, so a poll that
finds nothing new costs one round trip.

### `pbp_participation` is deliberately NOT read

`docs/collectors.md` settles the play-by-play *format* for every collector and
explicitly leaves the participation feed open for this one: *"that feed meets
the 46.82 MiB question on its own terms, and the answer there is the
field-per-megabyte one."* Answered, measured against the live 2025 artifacts:

- Its `route` column is a **single scalar per play** — the route run by the
  targeted receiver. It cannot say how many routes each *position* ran on a
  snap, so it cannot supply `opportunities_defended` as routes-covered.
- Its `offense_positions` is the **roster-listed** position of each of the
  eleven players on the field. That is the same fact `players.csv` holds once
  per player, repeated once per snap across ~45,000 snaps. It is not alignment.

So its entire return is a per-player position map, and `players.csv.gz` is that
map at 1/20th the bytes. Reading it would have made this collector's pass
67.43 MiB, of which 69% bought information already held.

### Freshness was checked before format

The rule `player-contract` earned — a `.csv.gz` can be abandoned while the
`.parquet` beside it rebuilds daily. Checked live on 2026-08-01 via the
releases API:

| release | `.csv.gz` | `.parquet` | verdict |
|---|---|---|---|
| `pbp` (2025) | 2026-02-12T09:58:23Z | 2026-02-12T09:58:23Z | in step |
| `pbp_participation` (2025) | *(none published)* — `.csv` 2026-02-10T18:54:08Z | 2026-02-10T18:54:07Z | in step |
| `players` | 2026-08-01T09:55:17Z | 2026-08-01T09:55:18Z | in step |

No divergence, so the ordinary size rule applies and `.csv.gz` wins on both
(`play_by_play_2025.parquet` is 19.40 MiB — *larger* than the 18.22 MiB gzip).

## Two things this collector does that the spec's field table does not say

Both are disclosed rather than silent. Neither is a guess.

### 1. `alignment` is always `"all"`

The spec's enum is `slot | perimeter | receiving_back | early_down_back |
inline_te | detached_te | all`. **Only `all` is sourceable**, so this is a
narrowing *within* the spec's own vocabulary rather than a departure from it.

`alignment` means where a player lined up **on the snap in question**, and no
nflverse feed carries a per-snap alignment column. `pbp_participation` gives
`offense_personnel`, `offense_formation`, `offense_players`,
`offense_positions`, `route`, `defense_man_zone_type` and
`defense_coverage_type` — and `offense_positions` is the roster-listed
position, not where the player stood. NGS receiving has nothing finer.

The spec anticipates this and names the wrong answer outright:

> The hard part is alignment classification: without per-snap alignment the
> slot/perimeter split degrades into a season-long player label applied
> retroactively to every snap, **which is wrong for any receiver who moves.**

So the sub-splits are not synthesised. **To unlock them** an upstream would
have to supply a per-snap alignment or pre-snap-position column: a charting
provider (PFF, SIS) or an NGS tracking feed exposing receiver x/y at the snap.
When one is wired, `scoring.ALIGNMENTS` grows and everything downstream —
`declared_splits()`, the coverage predicate, the schema enum — follows from it.

No deferred sibling collector is proposed. The sub-splits are a *refinement of
a row that already exists* rather than a separate signal, unlike the
`coaching-staff` and `player-incentives` narrowings earlier in Phase 8.

### 2. On a `DST` row, `team_id` is the conceding **offense**

The spec glosses `team_id` as "the abbreviation of the *defense* the row
describes". That holds for QB, RB, WR, TE and K. It cannot hold for `DST`,
because **a defense never faces a DST** — there is no quantity "fantasy points
a defense allows to the DST position". The conceding unit for that one enum
member is the team's offense: sacks taken, turnovers given up, defensive and
return touchdowns conceded, and its own final score against the tier ladder.

The invariant that does hold for all six is ***`team_id` is the team that
concedes***, and the lookup is unchanged either way: a generator projecting
team A's DST against team B reads `(team_id=B, position=DST)`, exactly as it
reads `(team_id=B, position=WR)` for a receiver. The alternative — dropping
`DST` — would remove a sourceable, wanted row over a wording problem.

`opportunities_defended` on a DST row is offensive plays run.

## The named failure mode, and what the guard actually does

> Defenses that build leads face pass-heavy opponents in the fourth quarter, so
> a strong defense accumulates inflated per-game WR and TE allowance while its
> per-opportunity allowance stays elite — the raw rating then reads as a soft
> matchup precisely for the teams that are hardest to score against.

Every field is populated and every value plausible, so a null check cannot see
it. The guard is the spec's: rank the 32 defenses on each basis within a split
and **flag** — never drop — any team whose two ranks differ by more than eight
places. A flagged row is published with `rank_divergence_flagged: true`, files
a `rank_divergence` entry in `coverage.errors`, and increments
`defense_vs_position_rank_divergences`.

### It was checked against a shuffled null before it was trusted

A statistic can be dead. Measured on the real **2025 regular season** through
this exact code path, against a null of two independent rankings of 32 teams
(400 shuffles per split):

| position | flagged / 32 | shuffled null | observed ÷ null |
|---|---|---|---|
| QB | 2 (6.2%) | 54.6% | **0.11** |
| RB | 3–4 (10.4%) | 53.9% | **0.19** |
| WR | 6–7 (19.8%) | 54.2% | **0.37** |
| TE | 6–8 (21.9%) | 53.6% | **0.41** |
| K | 12 (37.5%) | 54.7% | **0.69** |
| DST | 0 (0.0%) | 54.2% | **0.00** |
| **all** | **92 / 576 (16.0%)** | **54.2%** | **0.29** |

Two independent rankings flag 54% of the league by construction, so that is
the noise floor. Every position beats it. On the two positions the spec's
failure mode actually names — WR and TE — the guard flags six to eight of
thirty-two, not twenty.

It also fires in the **right direction**. On WR/PPR in 2025:

| team | per-game rank | per-opportunity rank | reading |
|---|---|---|---|
| BAL | 4 | 23 | looks soft per game, elite per opportunity |
| IND | 2 | 19 | looks soft per game, elite per opportunity |
| CIN | 31 | 15 | looks tough per game, soft per opportunity |
| MIA | 19 | 4 | looks tough per game, soft per opportunity |

The first two are the spec's sentence exactly.

**Caveat, stated rather than tuned away: `K` is the weak arm at 0.69.** A
kicker's per-opportunity rate is dominated by the field-goal/extra-point mix
and its per-game rate by volume, so the two bases genuinely measure different
things and the guard fires more often than it is informative. It is still
comfortably better than chance, and the threshold is **not** recalibrated per
position — a threshold fitted to today's distribution is a filter on tomorrow's
signal, which is why `team-scheme` rejected one. Eight is the spec's number.

## The opponent adjustment

`fantasy_points_allowed_per_game_adj = fantasy_points_allowed_per_game /
opponent_strength_index`.

- **Fit on offensive units, never on prior defensive ratings.** The spec
  forbids the alternative and the reason is circularity: a defensive rating is
  already a function of the offenses it faced.
- **Leave-one-out.** An opponent's strength, as used to adjust the defense it
  played, is computed from that opponent's *other* games — so a defense that
  shut an offense out is not told that offense is weak partly because of the
  shutout. An offense with one game falls back to it, and that residual is
  stated rather than hidden.
- `adjustment_method` is `opponent_offense_mean_ratio_loo_v1` — it names the
  arithmetic performed, not a model this collector does not implement.
- `adjustment_window_weeks` is the real number of distinct weeks in the sampled
  play set, because the ratings and the adjustment are fit over **one** play
  set rather than two.

## Both bases from one play set

Required by the spec, and structural here rather than remembered: `games` and
`opportunities` are two fields of the same `StatLine`, incremented by one pass
over the same plays. So `per_game × games == per_opportunity × opportunities`
for every row, which is what
`test_both_bases_share_one_numerator` asserts across all 576.

## Identity

Every allowed opportunity resolves to a canonical `player_id` through
`IdentityClient.resolve_many` **before** it is attributed to a position;
`pbp.totals_from_fold` is that boundary. `resolve_many` chunks internally at
`BATCH_LIMIT` (500) — there is no caller-side batching. A player
`player-identity` declined to name contributes nothing, under any id.

`player_id` appears in **no published field** — rows are keyed by (defense,
position, alignment, scoring_format). Identity is a gate, not an output.

A dropped player deflates a defense's rates without removing its row, which
coverage cannot see, so `defense_vs_position_players_resolved_ratio` is the
series that makes it visible and one summarised `identity_unresolved` entry
lands in `coverage.errors`.

### A live `player-identity` hazard, found and reported

`player_identity.api.build_query` raises `HTTPException(422)` for a position
outside `KNOWN_POSITIONS`, and `resolve_queries` calls it **inside** the loop
over the batch — so **one unmapped position code fails the whole 500-query
request**, not one row of it.

That reproduces against this collector's own upstream: nflverse `players.csv`
publishes 25 distinct position codes, of which exactly one — **`SAF`, 345
players** — is absent from `KNOWN_POSITIONS` (which carries `S`, `FS` and `SS`
but not `SAF`). Three players with 2025 opportunities carry it.

This collector does not hit it, because `ROSTER_TO_FANTASY` narrows to six
codes before anything is resolved and all six are known. That is a
*consequence* of another decision, so `SENDABLE_POSITIONS` makes it an
assertion instead: an unrecognised code travels as `position: None`, costing
one scoring signal for one query rather than 500 resolutions. **The underlying
`player-identity` behaviour is not fixed here** — it is a batch-endpoint
robustness question for that service, reported rather than worked around.

## Coverage

`expected` is **32**, declared as a constant. A defense counts **present** only
when every one of its 18 declared splits exists with `games_sampled >= 1`; a
row that exists with `games_sampled == 0` is a placeholder, not a datum, and
files an `incomplete_splits` entry.

Both halves are tested independently — `test_a_truncated_upstream_does_not_
report_full_coverage` attacks the floor and
`test_a_defense_with_empty_splits_is_not_present` attacks the predicate. A
mutation set that only attacks the first scores well and misses the second.

## Cost

**20.61 MiB per changed pass**, base interval one day, conditional GET on both
feeds so an unchanged poll costs one round trip.

**`CAPTURE_ENABLED=false`**, and the decisive argument is not the steady-state
bandwidth:

1. `CAPTURE_SEASON` is `2026` and **`play_by_play_2026.csv.gz` 404s today** —
   the season has not started. With the loop on, every pod start would write a
   `present: 0` failure envelope and hold `collector_coverage_ratio` at 0.0
   indefinitely: a permanently red gauge, for zero data, which is how operators
   learn to ignore a gauge.
2. The Kind cluster is recreated on every `integration-test` run and every pod
   restart re-captures, so `true` means 20.61 MiB from GitHub release assets
   per CI run.
3. **The guard is not vacuous without a running loop.** `broadcast-context`
   ships `true` only because its own guard cannot be exercised without one;
   this collector's rank-divergence guard runs inside `capture`, is driven by
   155 tests against a synthetic 32-team season, and was measured against the
   real 2025 artifact offline. Nothing here needs the loop to be provable.

Flip it to `true` once `CAPTURE_SEASON` names a season whose artifact exists.

There is no `smoke.sh`: this collector adds no route beyond the standard five,
which `scripts/smoke-test.sh` asserts for every registered collector already.
A hook must not `POST /refresh` while `CAPTURE_ENABLED=false` anyway — a
dispatched refresh reaches nflverse regardless of the flag.

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health`
and `/metrics` requires `Authorization: Bearer <token>`.

`GET /signals` filters on `season`, `week`, `signal_type` (universal) plus
`team_id`, `position`, `alignment`, `scoring_format` and
`rank_divergence_flagged`. The full row set is ~576 rows, so the filters are a
convenience rather than a boundary.

`POST /refresh` returns **202 — accepted, not done**. Poll `/signals`; do not
read it on the next line.

## Metrics

Beyond the fleet-wide `collector_*` series:

| series | why coverage cannot see it |
|---|---|
| `defense_vs_position_rank_divergences` | a divergent defense has a complete set of populated rows |
| `defense_vs_position_players_resolved_ratio` | a dropped opportunity deflates a rate without removing a row |
| `defense_vs_position_rows_captured` | ordinary volume |

## Tests

```bash
cd services/defense-vs-position
uv run pytest -v
```

The upstreams are mocked at the **wire** with `respx`, serving real gzipped
CSVs built by `tests/season.py`, so the streaming path, the gzip trailer check,
the column projection, header validation, the conditional-GET commit and
`resolve_many`'s chunking are all inside the suite.

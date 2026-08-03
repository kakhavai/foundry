# offensive-line

A Foundry signal collector. See [`docs/collectors.md`](../../docs/collectors.md)
for the authoring guide and
[`docs/architecture/phase-8-data-source-collectors.md`](../../docs/architecture/phase-8-data-source-collectors.md)
for the fleet plan.

| | |
|---|---|
| Port | `8027` |
| Gateway path | `/collectors/offensive-line` |
| Cadence class | `weekly` |
| Signal types | `offensive_line_strength` |
| Depends on | `player-identity` (starter rows only) |
| Scope-aware | no — offensive linemen are not fantasy-scored, so `roster-scope`'s watchlist selects nothing here |
| Capture loop | **off** — three of its six feeds 404 for the configured season; see below |

## What it captures

How much protection and running room an offence's front five actually
provides: the quarterback's sack risk, and the share of a running back's
production that comes free rather than after contact. It is the only collector
that tracks **unit continuity** — how many consecutive games the same five
have started — and the measured drop-off when one of them is replaced, so a
generator can discount a line that grades well on tape it will not repeat with
its current personnel.

Two row shapes under one signal type, discriminated by `record_type`:

* **`unit`** — one per offence. Every rate: `pressure_rate_allowed`,
  `sack_rate_allowed`, `adjusted_line_yards`, `mean_time_to_throw`, their
  opponent-adjusted counterparts, plus `lineup_hash`, `continuity_games` and
  the replacement correction described below.
* **`starter`** — five per offence. `starter_id` (canonical, from
  `player-identity`), `starter_position` (`LT`/`LG`/`C`/`RG`/`RT`),
  `starter_snap_share`, `starter_availability` for the upcoming week, and
  `replacement_delta_pressure_rate` **with its provenance**.

Two fields on the unit row are additions to the spec's table, and both are
required by claims the spec makes elsewhere: `pressure_rate_allowed_adj_observed`
(the strictly symmetric pairing term, since `pressure_rate_allowed_adj` carries
the replacement correction) and `lineup_change_known` (see the guard). The
starter row adds `replacement_delta_provenance` and
`replacement_delta_sample_games`, required by the adapter note. **Whoever owns
the generator needs to be told about all four.**

`coverage.expected` is the spec's own clause taken literally: 32 offences x
(one unit row + five starter rows) = **192**. A team with fewer than five
identified starters is reported in `coverage.missing` rather than emitted
partially.

### Paired with `defensive-front`

The two are a matched pair, and the generator's matchup feature is a plain
subtraction — `front.<metric>_generated_adj − line.<metric>_allowed_adj`,
joined on `(season, week, team_id)`, **with no unit conversion**. The spec
warns that a divergence "silently corrupts the differential rather than
failing", so agreement is enforced rather than intended:
[`tests/test_scale_agreement.py`](tests/test_scale_agreement.py) reads
`defensive-front`'s own `ratings.py` **by AST** and

* compares every Football Outsiders line-yards constant,
* compares the two `line_yards` curves numerically at every tenth of a yard
  from a 20-yard loss to a 99-yard gain,
* compares `opponent_strengths`, `_faced_strength` and `_adjust` **statement
  for statement**, and
* asserts `pass_block_snaps` here is literally the same number
  `pass_rush_snaps` is there, counted from opposite sides of one intersected
  play set.

`pressure_rate_allowed_adj` additionally carries the replacement correction
below, so `pressure_rate_allowed_adj_observed` is published beside it as the
strictly symmetric term. A consumer differencing the pair picks whichever it
means; the schema field descriptions say which is which.

## Upstreams

Six nflverse feeds, **~78.2 MiB on a changed pass**. Freshness was re-checked
across formats against the releases API *before* size, per
`player-contract`'s finding that an abandoned artifact passes every size rule;
every format of every feed below shares a timestamp, so the fleet rule applies
and takes the `.csv.gz` where one exists.

| feed | size | fatal? | what its loss costs |
|---|---|---|---|
| `play_by_play_<season>.csv.gz` | 18.22 MiB | yes | everything |
| `pbp_participation_<season>.csv` | 46.82 MiB | yes | every pressure column |
| `depth_charts_<season>.csv.gz` | 10.15 MiB | no | the starter half |
| `players.csv.gz` | 2.39 MiB | no | the starter half (the crosswalk) |
| `snap_counts_<season>.csv.gz` | 0.48 MiB | no | the starter half |
| `injuries_<season>.csv.gz` | 0.12 MiB | no | `starter_availability` |

Losing any of the middle three costs five of six rows per team, which coverage
states loudly (32/192, ratio 0.167) rather than hiding. Every unit rate is
unaffected, and the spec says the unit row is what drives the projection.

**Conditional GET is on for every feed.** A `304` is a *successful* capture:
`last_capture_at` advances and no envelope is written. A `304` on one feed
never suppresses another that changed, and the feeds that answered `304` are
re-fetched **through the same failure classifier** the first request went
through — both are live bugs from elsewhere in this fleet and both have a test
here.

### The join, and why `players.csv` is not optional decoration

`snap_counts` is keyed by `pfr_player_id`. `depth_charts` is keyed by
`gsis_id`. Nothing joins them directly, and `players.csv` is the only free
document carrying both — measured on the real 2025 regular season, **4,195 of
4,212** offensive-line snap rows (99.6%) cross-walk successfully. A name-based
join was rejected: two linemen sharing a surname on one roster is not
hypothetical, and a wrong join silently attributes one man's snaps to another
and changes the lineup hash.

**Know what the 0.4% is, because it is one man and he starts.** On 2025 the
un-crosswalkable rows are all **Alec Anderson (BUF)** — he has a `gsis_id`
(`00-0037428`) but a blank `pfr_id` in `players.csv`, so `snap_counts` cannot
name him. He played **74 of 74 offensive snaps in week 13 and 75 of 75 in week
18**: two full starts that leave Buffalo's five unidentifiable for those
games, and therefore leave the *following* week unable to tell a stable line
from a changed one. That state is handled — see the guard below — but it is
the kind of thing worth finding in a README rather than in a wrong number.

### A September consequence of `MIN_DELTA_GAMES`

`MIN_DELTA_GAMES` is 2 on **both** sides of a with/without split, and the
positional prior is built from the same pass's measurements — so no
replacement delta of any kind exists before a team's fourth game. Every team
that changes its five in weeks 1-3 is dropped wholesale with
`lineup_changed_without_replacement_delta`, and **league coverage sits well
below 192 for the first month of a season**. That is the honest reading of a
window too short to measure anything in, not a broken collector. A
cross-season prior read back from the lake would fix it and is not built.

### `CAPTURE_ENABLED=false`, measured rather than scaffolded

Verified live against the nflverse releases API on 2026-08-03:
`play_by_play_2026.csv.gz`, `pbp_participation_2026.csv` and
`snap_counts_2026.csv.gz` **do not exist** — the season has not been played.
Only `depth_charts_2026` is published. A running loop would fail on its first
fatal feed every pass and pin `collector_coverage_ratio` at 0.0 forever, for
zero data. The cost argument stands on its own besides: ~78.2 MiB against a
third party on a cadence, re-incurred on every CI cluster rebuild and every
pod restart. See `helm/values/offensive-line/values.yaml`.

## The failure mode, and the guard that fails on it

> When a starter goes to IR mid-week, the unit's aggregate grades do not move,
> because they are computed over snaps the departed player took — the line
> keeps its elite `pressure_rate_allowed_adj` into the exact week it will be
> worst. **Nothing about the row looks malformed.**

Every field is populated, every value is in range, coverage is 1.0, the schema
validates and the differential computes cleanly. So
[`guard.py`](offensive_line/guard.py) runs a cross-field assertion over
exactly the rows that are about to be published: whenever `lineup_hash`
differs from the prior game's, it requires `continuity_games == 0`, a non-zero
replacement correction, and `pressure_rate_allowed_adj` to equal
`pressure_rate_allowed_adj_observed + lineup_adjustment_pressure_rate`.

**It fails the pass rather than flagging it**, unlike `defensive-front`'s
timing guard. That guard renders a statistical verdict about a league-week and
has a false-positive rate; this one asserts an arithmetic invariant over rows
this process just built, so a violation can only be a defect in the collector.
`fail_capture` writes one `present: 0` envelope with the reason and re-raises,
leaving the last good capture serving on `/signals`.

**Three states, not two — and the third is the one that bites.**
`lineup_changed: false` is only safe when both this game's five and the prior
game's were identified. When either could not be, the five may have moved, no
correction was computed, and the adjusted rate is the one the *departed*
players earned. Reproduced against this fixture: blinding one slot of the
prior game published `pressure_rate_allowed_adj` **51% high**, with coverage,
the row's own `lineup_hash`, its five starter rows and its schema validity all
identical to a healthy pass — the row's hash is fine, it is the *prior* hash
that is missing, and that one is not on the row. So the unit row carries
`lineup_change_known`, and when it is false **`pressure_rate_allowed_adj` and
`lineup_adjustment_pressure_rate` are both null** with the reason in
`null_field_reason`. Everything the uncertainty does not touch — the raw
rates, `pressure_rate_allowed_adj_observed`, `adjusted_line_yards`,
`mean_time_to_throw` — still publishes, which is why this is a null rather
than a dropped team. The guard enforces it: a row that could not tell and
publishes a corrected rate anyway raises.

The neighbouring case is **not** a crash. "The lineup changed and this pass has
no replacement delta to correct it with" is missing input data, so that team —
unit row included — is dropped into `coverage.missing` with the reason
`lineup_changed_without_replacement_delta` and the rest of the league still
publishes. Both paths honour "a stale unit must not publish"; only one of them
is a defect.

### `lineup_hash` is decided by snaps, never by the depth chart

The spec is explicit — "or continuity becomes a description of the team's
press releases". `snap_counts` decides **who actually played**; `depth_charts`
supplies only the **slot label** the hash is ordered by, read from the
snapshot current at that week's last game rather than the newest in the file.
`depth_charts` carries no season or week column at all (219 daily snapshots
across the 2025 release), so the calendar comes from play-by-play's
`game_date`. Labelling a week-5 lineup from the March chart would reorder the
hash and report churn on lines that never changed.

## Honest nulls

Three, and each is a null with a machine-readable reason rather than a zero, a
default or a synthesised split.

1. **`yards_before_contact_per_carry` and its `_adj` are `null`**, with the
   reason on every row in `null_field_reason` and `"type": "null"` in the
   contract so a later "fill-in" fails conformance. PFR publishes yards before
   contact at season level, so it cannot be attributed to a week or to the
   front faced, and nothing free publishes it per play. Deliberately **not**
   derived from `adjusted_line_yards`, which measures a different thing.
   **Symmetric with `defensive-front`**, which nulls its field of the same
   stem — a differential where one term is real and the other is null looks
   computable and is not, which is worse than one where both are null. A test
   compares the two collectors' null sets so they cannot drift apart.
2. **`replacement_delta_pressure_rate` says how it was arrived at.**
   `measured` is a with/without split of that team's own window games, at
   least two on each side, on the **opponent-adjusted** per-game rate — a raw
   split is confounded by which fronts happened to fall in each half, and on
   this collector's own fixture that produced a delta of the wrong sign
   before the adjustment was added. `league_positional_prior` is the mean of
   the measured deltas at that slot across the league in the same pass,
   restricted to men who are **currently starting**. That restriction is a
   disclosed modelling assumption and it is survivorship-biased — a starter
   who was replaced and did not get the job back is excluded by construction,
   so the pool leans toward changes that reverted. The alternative is provably
   worse: pooling deputies too cancels the two sides of every substitution to
   approximately zero, which is a claim that losing a starter is free. The
   assumption is on the schema field. `unavailable` means neither existed and
   the value is `null`.
3. **`starter_availability`'s `ir` is not in the injury report.** Verified on
   the real 2025 season: `report_status` carries only `Out`, `Questionable`,
   `Doubtful` and blank across 6,068 rows. `ir` is a roster designation and
   comes from `players.csv`'s `RES`/`PUP`; the two feeds are merged with
   roster status winning, because a man on injured reserve is not merely
   doubtful.

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

`GET /signals` accepts `season`, `week`, `signal_type` (universal) plus
`team_id`, `record_type` and `starter_position`. Anything else is 422.

**Plus one:** `GET /lineups?season=&week=&team=` — the projected starting five
and its continuity for an upcoming week, with `unavailable_starters` naming
the men a generator should not project as playing. Forward-looking, and
therefore not derivable from the realized `/signals` history without already
knowing which of its fields look forward. Published in
`helm/values/offensive-line/values.yaml` under `gateway.publicPaths`.

## Tests

```bash
cd services/offensive-line
uv run pytest -v
```

**203 tests**, ~97% statement coverage. The count is honest: it includes
eleven malformed-row tests, and several entries are parametrised cases of one
claim rather than independent assertions.

The suite is **mutation-tested**, across two rounds and an independent review.
**101 hand-designed mutants, 95 killed** — 93 against this collector (the
guard, the continuity walk, both halves of the coverage predicate, the
opponent adjustment, the scale constants, the join, the lineup derivation,
identity, the replacement delta, the deliberate nulls, conditional GET, the
digest gate and the routes) and eight against **`defensive-front`**, because
the pairing guarantee is only real if a change on the *other* side of the
subtraction fails here. The six survivors are equivalent, each verified by
applying a non-equivalent neighbour and watching the suite kill that instead;
two whose equivalence is a property worth knowing carry a note at the site
(`capture._strength_envelope`'s `acc.expect` block and
`lineups.derive_lineups`' crosswalk miss).
[`tests/test_survivors.py`](tests/test_survivors.py) is the record of what
earlier rounds left undefended — every test in it names the mutation it kills.

Two rounds of this found real defects rather than only test gaps: the
replacement delta measured on the raw rate (wrong sign), the prior pooled to
exactly zero, and — from the review — a `test_scale_agreement.py` that
reconstructed `defensive-front`'s curve instead of executing it, so three
mutations of the sibling's `line_yards` (including one zeroing every carry
past ten yards) passed silently.

The fixture is the part worth reading before touching anything —
[`tests/season.py`](tests/season.py). Every pressure is generated as
`f(offence) + g(defence)`, because a fixture varying production by one term
alone makes a *correct* opponent adjustment remove 100% of the variance and
collapse to the league mean, which is bit-for-bit identical to the
constant-valued bug that shipped on `defense-vs-position`. Beyond that, four
of its structural choices exist because a mutant survived without them:

* **two kickoff dates a week and three chart snapshots, one on game day** —
  with one game date a week and every chart exactly three days earlier, the
  per-week chart lookup and the latest-game-in-the-week rule were both
  indistinguishable from their opposites, and the real feed is 219 *daily*
  snapshots against a Thursday-to-Monday slate;
* **a three-man snap tie with the winning id in the middle of the row order** —
  with two tied men, "highest id" coincides with "first row seen" or "last row
  seen" for free, and a row-order tie-break survives half the time;
* **a lineman who plays zero snaps and is the only candidate for his slot** —
  without one, "a man who did not play cannot fill a slot" is unfalsifiable;
* **penalty-nullified `no_play` rows** charted as pass rushes, so the
  intersection the whole join exists for is exercised.

Add to that: the schedule is deliberately unbalanced, half the league changes
its left tackle mid-window, one team swaps its guards mid-season, one is
missing a depth-chart label, and one carries a man on injured reserve who is
*also* on the weekly injury report.

Everything but the socket is real: the streaming parser, the gzip inflater and
its truncation check, the conditional-GET protocol, the six-feed join, the
`pfr_id`/`gsis_id` crosswalk, the leave-one-out adjustment, the lineup guard
and the digest gate.

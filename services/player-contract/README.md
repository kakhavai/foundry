# player-contract

A Foundry signal collector. See [`docs/collectors.md`](../../docs/collectors.md)
for the authoring guide and
[`docs/architecture/phase-8-data-source-collectors.md`](../../docs/architecture/phase-8-data-source-collectors.md)
(`#### player-contract`) for the spec.

| | |
|---|---|
| Port | `8024` |
| Gateway path | `/collectors/player-contract` |
| Cadence class | `seasonal` |
| Signal types | `player_contract_status` |
| Scope-aware | yes — narrows to `roster-scope` membership before fetching |
| Capture loop | **disabled** (`CAPTURE_ENABLED=false`) — see below |
| Upstream | `historical_contracts.parquet` — **not** the release's abandoned `.csv.gz` |

## What it captures

What a player is financially committed to and for how long. The sourceable,
load-bearing part is **contract year**: a player in the final season of a deal
is in a measurably different situation from one with three years left, and that
is visible from nothing else in the fleet — not box scores, not depth charts,
not news.

One upstream: nflverse's `contracts` release
(`historical_contracts.parquet`, sourced from OverTheCap). 6.44 MiB on the wire,
51,785 rows of which 2,931 are flagged active. Read with column projection and
conditional GET, filtered to active contracts in Arrow before a single row
becomes a Python object.

Against the live document today: **2,930 records, 1,273 of them in a contract
year** — which is the signal this collector exists for.

## The upstream: parquet, because the CSV artifact is abandoned

nflverse rebuilds the `contracts` release daily. It stopped regenerating that
release's **CSV** four years ago. Measured 2026-08-01:

| asset | last updated |
|---|---|
| `historical_contracts.csv.gz` | **2022-05-29** |
| `historical_contracts.parquet` | 2026-08-01 09:11 |
| `historical_contracts.rds` | 2026-08-01 09:11 |
| `timestamp.json` | `2026-08-01 05:11:43 EDT` |

The documents agree with their own timestamps. The CSV's newest `year_signed`
is 2022 and **2,869 of its 2,887 "active" contracts had already expired**; the
parquet's newest is 2026, with 1,793 of 2,931 active deals signed this year.
Nothing raises on a stale document — an earlier revision of this collector read
the CSV, passed its whole suite, and published four-year-old contracts as
current.

So this collector reads `historical_contracts.parquet` (6.44 MiB, 51,785 rows),
with **`pyarrow` in this service's `pyproject.toml` and nowhere else**.

That is a bounded exception to `docs/collectors.md`'s CSV-over-parquet rule,
not a repeal of it. That rule is a *size* argument decided on the play-by-play
feed, where the parquet is genuinely larger than the gzipped CSV; it assumes
both formats are current, which here they are not. The rule has been amended to
say **compare `updated_at` across formats before comparing sizes**, and the
whole nflverse dependency set was audited when this surfaced — `contracts` is
the only release in it with format-divergent staleness.

### Three things about the parquet that the CSV did not have

**Money is in MILLIONS.** The CSV carried whole-dollar integers; the parquet
carries doubles denominated in millions (Mahomes: `value = 448.0`). Converted
once, in the adapter, and published as whole USD. Verified lossless across every
active row. Getting this wrong is wrong for every row at once, at a plausible
magnitude, and raises nothing — which is why `to_usd` has its own unit tests and
why the fixtures are in millions too.

**A per-season cap table.** The CSV's always-empty `season_history` column is
replaced by a populated `cols` list-of-structs. Two of the phase doc's six
"null by necessity" fields come from it — see below.

**`gsis_id` on 76.8% of active rows** — a Tier-1 published crosswalk key, which
changes how identity resolution works here. See below.

### The staleness instrumentation is still here, as a backstop

Built when the CSV was the upstream, kept because it is the thing that would
catch this happening again:

* **`seasons_remaining` is not clamped at zero**, so a deal whose final season
  precedes the capture season reads negative and cannot be mistaken for a live
  contract.
* **`player_contract_expired_records`** counts them and the envelope carries a
  priority error, `contract_end_season_precedes_capture_season`.
  `collector_coverage_ratio` structurally cannot see this: an expired deal is a
  *present* record with a non-null `contract_end_season`.

Against the live parquet it reads **33 of 2,930 (1.1%)**, down from 2,869 of
2,887 (99.4%) on the CSV. The remainder are genuinely expired deals the upstream
still flags active — real data quality, not staleness.

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health` and
`/metrics` requires `Authorization: Bearer <token>`.

**No extra routes.** The incentive query route (`GET /signals/incentives`)
belonged to the deferred half — see below — so there is nothing to publish
beyond the standard surface, and no `smoke.sh`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

`/signals` filters: `player_id`, `team`, `is_contract_year` — plus the universal
`season`, `week`, `signal_type`. `is_contract_year` is **tri-state**: a row whose
term the upstream did not supply carries `null` and matches neither
`?is_contract_year=true` nor `=false`, because "we do not know" is not an answer
to either question.

## The incentive half is deferred, deliberately

The spec originally paired this collector with a second signal type,
`player_incentive_progress`, and it was split during 8E into a
`player-incentives` collector that is **not built**.

**The feed carries bonus amounts without thresholds.** The parquet's per-season
table has `prorated_bonus`, `roster_bonus`, `workout_bonus`, `option_bonus`,
`other_bonus` and `per_game_roster_bonus` — so the blunter claim, that no column
anywhere mentions `bonus`, is true of the dead CSV and **false** of the live
parquet.

It does not change the answer. Those are contractual cash buckets the deal
allocates by year; none carries the three things `player_incentive_progress` is
defined by — a `metric` from the enum, a `threshold` to measure distance
against, and an LTBE/NLTBE classification. `per_game_roster_bonus` is the
closest and still has neither a threshold nor a classification.

The split matters because the collector would compute only half of each record.
`current_progress` is derivable by joining against the statistics collectors;
the **thresholds** must come from the adapter, and there is no free source for
them. A collector that can compute progress against thresholds it does not have
emits nothing. `tests/test_capture_contract_conformance.py` pins that no
incentive surface has crept back in.

## Four fields are null, for two different reasons

The phase doc lists six cap-accounting fields as null by necessity. That was
true of the CSV. The parquet's per-season cap table supplies two of them:

| field | source | coverage |
|---|---|---|
| `cap_hit_current_usd` | `cols[year == season].cap_number` | 75.7% of active rows |
| `signing_bonus_proration_usd` | `cols[year == season].prorated_bonus` | 59.8% |

Both are direct lookups keyed by year, requiring no derivation. **This is a
deliberate expansion of the spec's field set**, and it was not optional:
`null_field_reasons` makes a machine-readable *claim about the source*, and
continuing to emit `unsourced_by_upstream` for a field the document supplies on
2,218 rows would write a falsehood into an append-only lake. A wrong reason is
worse than a missing field.

The remaining four are null on every row, and the reasons differ:

```json
"cap_hit_current_usd": 30250000,
"dead_money_if_cut_usd": null,
"guaranteed_remaining_usd": null,
"null_field_reasons": {
  "dead_money_if_cut_usd": "unsourced_by_upstream",
  "tag_status": "unsourced_by_upstream",
  "void_years_count": "unsourced_by_upstream",
  "guaranteed_remaining_usd": "requires_undefined_derivation"
}
```

* **`unsourced_by_upstream`** — no column exists. `dead_money_if_cut_usd`,
  `tag_status`, `void_years_count`. A paid feed would be needed.
* **`requires_undefined_derivation`** — the components are present and the
  definition is not. `guaranteed_remaining_usd`: the per-season table carries
  `guaranteed_salary`, but "not yet earned" needs a rule about the in-progress
  season the source never states, and inventing one would be fabrication.
* **`absent_in_upstream_row`** — this row did not say. Any row-nullable field,
  including the two sourced cap fields on the 24% of active rows with no entry
  for the capture season. Distinct from the first: it tells a consumer that
  buying a feed would not help *for this row*, while the source itself is fine.

The schema types the four permanently-null fields as `"null"` rather than
`["integer", "null"]`, so fabricating one is a contract violation caught by the
conformance test rather than a code-review question. **None is derived from
`apy`** — average annual value is not a cap hit, and the two agreeing on some
rows is exactly what makes the substitution dangerous, so the column is not in
`COLUMNS` and is never read.

### The `Total` pseudo-row

The per-season table's **last element is not a season**. Every one of the 2,250
active rows carrying a cap table ends with a row whose `year` is the literal
string `"Total"` and whose `cap_number` is the *career* total — Joe Burrow's is
$339,443,060 against a 2026 cap hit an order of magnitude smaller. So `cols[-1]`,
the obvious "give me the latest" shortcut, publishes a career total as a
current-season cap hit for every player in the league, at a magnitude plausible
enough to survive a spot check. The lookup matches the year exactly, and two
tests pin it.

## Money: nominal, never inflated

`value` and `guaranteed` are converted from the document's millions to whole USD
and emitted nominal. `guaranteed = 0` is a real fact about 778 active rows and is
not mapped to null.

The feed also carries `inflated_value` / `inflated_apy` /
`inflated_guaranteed` — OverTheCap's restatement into present-day dollars,
computed against a cap-inflation index this collector cannot see, version or
reproduce. That index moves, so the same historical contract would yield a
different number next season, making an append-only lake object
un-reproducible from its own `captured_at`. Confirmed against the live parquet:
they equal `value` on the 1,793 deals signed this season and diverge on the other
1,138 (Burrow, signed 2023: value 275.0, inflated 368.46). **The inflated columns
are not read.** Nothing here mixes the two.

## Identity: two arms, because only 77% of rows carry a crosswalk key

`player` is a display name (`"Aaron Rodgers"`), not an id. The parquet also
carries `gsis_id` — a **published crosswalk source** `player-identity` adopts at
Tier 1 with no attribute scoring — on 2,250 of 2,930 active rows. So there are
two query shapes:

* **`gsis_id` present (76.8%)** — `source`/`source_id` and **no name**. This is
  `player-profile`'s rule: a GSIS id absent from the crosswalk would otherwise
  fall through to attribute scoring, and a feed that already carries a league id
  and is matched by name anyway is how two Josh Allens become one player.
* **`gsis_id` absent (23.2%)** — name, team and position, into Tier 3 weighted
  agreement. There is no other route for these rows.

Either way `resolved: false` is a miss with a reason — **never** an adopted raw
string. Three things specific to this collector:

* **A wrong `team` is worse than no team.** `team` carries 0.20 of the
  resolution weight and scores as *disagreement*. The feed writes nicknames
  (`Packers`), mapped to canonical abbreviations; the 64 active rows carrying a
  slash-joined multi-club string publish `team: null`, because the ordering is
  not consistent — `PHI/IND/WAS` (Wentz) is chronological while `IND/ATL` (Ryan)
  is reversed, so neither the first nor the last segment is reliably current.
* **An unmapped position 422s the whole batch.** `player-identity`'s
  `build_query` rejects a position outside `KNOWN_POSITIONS` and
  `/resolve/batch` validates the whole body, so one `ED` costs all 500 queries
  in its chunk — including every Tier-1 query travelling with it. Six of
  OverTheCap's eighteen codes (`ED`, `IDL`, `LT`, `RT`, `LG`, `RG`) are outside
  that vocabulary and cover **984 of the 2,931 active rows**. `OTC_POSITIONS`
  maps them; an unrecognised future code maps to `None` rather than passing
  through.
* **`ResolveQuery` has no `birth_date` field** though `player-identity`'s server
  accepts one. Moot here — `date_of_birth` is populated on 35 active rows — but
  noted as a gap in the shared seam.

`otc_player_id` is published on every row: OverTheCap's own stable key,
provenance and a seed for a future `otc` crosswalk source. It is **not** an
`fdy-` id, is never sent as a `source_id` (`otc` is not in `CROSSWALK_SOURCES`,
so it would be inert traffic dressed as a join), and the schema pins `player_id`
to `^fdy-` so a swap fails the conformance test.

## Coverage, and two disclosed deviations

The predicate: **every scoped player under an active NFL contract has a record
with a non-null `contract_end_season`.** `EXPECTED_FLOOR` is 384 — 32 teams x 12
individual `roster-scope` slots, a fact about the league's config decided before
any fetch.

**Deviation 1 — the spec's original `cap_hit_current_usd` clause is dropped.**
That field has no free source, so the original predicate would peg coverage at 0
for every player forever *and* destroy the ratio's ability to report anything
else, such as a truncated upstream. This is the clause-swallowing failure
`team-scheme` hit. Carried in the phase doc as well.

**Deviation 2 — practice-squad players and unsigned free agents are reported
missing rather than excluded from `expected`.** The phase doc asks for the
exclusion; it cannot be implemented from here. `ScopeClient` returns member ids
and nothing else, and reaching past it into the scope envelope's
`membership_status` column would fork the fleet's one narrowing seam — the thing
`durability-history`, `player-profile` and `usage-share` each explicitly refuse
to do. So a scope slot with no active contract row is failed under its own
reason, `no_active_contract`, which a consumer can subtract.

The distinction is arithmetically invisible in any case: the floor is 384 and
the scope is 384, so `expected` reads 384 either way. The only difference is
that this way the slot is *named* in `coverage.missing` with a reason instead of
vanishing.

## Failure mode to watch: a restructured deal

A mid-season restructure changes proration and dead money retroactively. The
lake is append-only, so an old snapshot stays correct-as-of its `captured_at`
and **must not be reconciled backward**.

The phase doc calls this latent "with the cap fields null" and says it becomes
live the moment a feed supplies them. **That moment has arrived** — not via a
paid feed but via the parquet's per-season table, which supplies
`cap_hit_current_usd` and `signing_bonus_proration_usd`. A restructure in week 8
rewrites week 8's proration upstream, and week 3's published envelope must keep
saying what was true in week 3.

The discipline is structural rather than a convention: nothing in the capture
reads a prior envelope back, and no reconcile-backward path exists to be reached
for. The only prior state the process keeps is the in-memory publish digest,
which is forward-only and never rewrites an object — a restructure changes the
digest, which publishes a *new* object alongside the old one. That is the
append-only behaviour working, not a duplicate.

## Cost, memory, and why the loop is off

6.44 MiB per changed pass, one feed, conditional GET — verified against the live
asset, which serves an `ETag` and answers `If-None-Match` with a `304` carrying
zero bytes while a ranged control returns `206`. Cheap; cost does not settle
`CAPTURE_ENABLED`. Two things do:

1. **`broadcast-context`'s vacuous-guard exception does not apply.** Nothing
   here reads a prior lake snapshot back, so one dispatched `POST /refresh`
   produces exactly what a season-long loop would.
2. **It would be the third collector reaching a third party from its loop**
   (today only `weather` and `broadcast-context` do), and it fails closed on the
   scope — so on a cluster where `roster-scope` has not yet published, every
   loop pass would write a `present: 0` envelope and count a capture failure.

A dispatched `POST /refresh` reaches the upstream regardless of the flag.

**Memory.** Parquet's footer-at-the-end layout means the body is buffered before
any row can be read, so this collector cannot stream the way the CSV ones do.
What it can do is filter in Arrow *before* materialising Python objects, and
that is worth 36 MB. Measured end to end against the live document, on the
256Mi (268 MB) pod limit:

| | peak RSS | headroom |
|---|---|---|
| `to_pylist()` then filter | 157.6 MB | 110.8 MB |
| filter then `to_pylist()` | **121.7 MB** | 146.7 MB |

48,854 of the 51,785 rows are historical, and `to_pylist()` on a whole batch
builds a dict — plus a list of nested cap-table dicts — for every one of them
before a single `is_active` is inspected. About 50 MB of the remaining peak is
baseline RSS for the interpreter with `pyarrow` imported, which is the standing
cost of the dependency and part of why it is scoped to this service.

## Tests

```bash
cd services/player-contract
uv run pytest -v
```

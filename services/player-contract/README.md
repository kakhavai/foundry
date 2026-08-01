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

## What it captures

What a player is financially committed to and for how long. The sourceable,
load-bearing part is **contract year**: a player in the final season of a deal
is in a measurably different situation from one with three years left, and that
is visible from nothing else in the fleet — not box scores, not depth charts,
not news.

One upstream: nflverse's `contracts` release
(`historical_contracts.csv.gz`, sourced from OverTheCap). 1.13 MiB on the wire,
5.79 MiB inflated, 31,893 rows of which 2,908 are flagged active. Read through
`stream_csv_dicts(gzipped=True, columns=...)` with conditional GET, filtering to
active contracts as the rows parse.

## Read this before you trust a row: the upstream CSV is frozen

Measured 2026-08-01 against the live release:

| asset | last updated |
|---|---|
| `historical_contracts.csv.gz` | **2022-05-29** |
| `historical_contracts.parquet` | 2026-08-01 |
| `historical_contracts.rds` | 2026-08-01 |
| `timestamp.json` | `2026-08-01 05:11:43 EDT` |

The release is refreshed daily; **the CSV variant is not regenerated**. The
document agrees with its own timestamp — the newest `year_signed` anywhere in it
is 2022, and `is_active` describes a 2022 roster.

So every record this collector publishes today is a contract as OverTheCap knew
it in May 2022. That is disclosed rather than papered over:

* **`seasons_remaining` is not clamped at zero.** A deal whose final season
  precedes the capture season yields a negative number, which cannot be misread
  as a live contract. Clamping would have made an expired 2021 deal identical to
  one in its final season, differing only in `is_contract_year`.
* **`player_contract_expired_records`** counts them, and the envelope carries a
  **priority** error, `contract_end_season_precedes_capture_season`.
  `collector_coverage_ratio` structurally cannot see this: an expired deal is a
  *present* record with a non-null `contract_end_season`, so coverage reads 1.0
  while every row is four years old.
* **`CAPTURE_ENABLED` stays `false`.**

**The parquet variant is not the fix.** `docs/collectors.md` settles the format
question fleet-wide: `pyarrow` is a 47.8 MiB wheel added to every collector
image, and parquet's footer-at-the-end layout forces the whole body to be
buffered before a single row can be read — reversing the streaming rule that
fixed `roster-scope`'s OOMKill. Swapping to a live CSV or JSON source is a
change to `UPSTREAM_URL` and `_to_row` and nothing else.

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

The free contracts feed carries **no incentive data at all**. Verified against
all 31,893 rows: the `season_history` column is empty on every one, and no
column or value anywhere in the document mentions `incentive`, `escalator`,
`LTBE`, `NLTBE` or `bonus`. This is not a sparse field, it is an absent one.

The split matters because the collector would compute only half of each record.
`current_progress` is derivable by joining against the statistics collectors;
the **thresholds** must come from the adapter, and there is no free source for
them. A collector that can compute progress against thresholds it does not have
emits nothing. `tests/test_capture_contract_conformance.py` pins that no
incentive surface has crept back in.

## Six fields are null by necessity

`cap_hit_current_usd`, `guaranteed_remaining_usd`,
`signing_bonus_proration_usd`, `dead_money_if_cut_usd`, `tag_status` and
`void_years_count` are cap-accounting derivations the free feed does not carry.

They are emitted **present and null** with a machine-readable reason rather than
omitted, so a consumer can distinguish "not supplied" from "not applicable":

```json
"cap_hit_current_usd": null,
"null_field_reasons": {
  "cap_hit_current_usd": "unsourced_by_upstream",
  "guaranteed_total_usd": "absent_in_upstream_row"
}
```

`unsourced_by_upstream` means **this source will never supply it** — a paid feed
would. `absent_in_upstream_row` means **this row did not say** — no feed change
helps. The two call for different consumer behaviour and an absent key expresses
neither.

The schema types all six as `"null"` rather than `["integer", "null"]`, so
fabricating one is a contract violation caught by the conformance test rather
than a code-review question. **None of them is derived from `apy`.** `apy` is
average annual value; a cap hit is not, and the two agreeing on some rows is
exactly what makes the substitution dangerous — so the column is not in
`COLUMNS` and is never read at all.

## Money: nominal, never inflated

`value` and `guaranteed` are whole USD integers already and are emitted
unchanged. `guaranteed = 0` is a real fact about 894 active rows and is not
mapped to null.

The feed also carries `inflated_value` / `inflated_apy` /
`inflated_guaranteed` — OverTheCap's restatement into present-day dollars,
computed against a cap-inflation index this collector cannot see, version or
reproduce. That index moves, so the same historical contract would yield a
different number next season, making an append-only lake object
un-reproducible from its own `captured_at`. **The inflated columns are not
read.** Nothing here mixes the two.

## Identity: the upstream keys by NAME

`player` is a display name (`"Aaron Rodgers"`), not an id. Every row resolves
through `player-identity` before it is emitted, and `resolved: false` is a miss
with a reason — **never** an adopted raw string. Three consequences that are
specific to this collector:

* It is the only scope-aware collector in the fleet with **no published
  crosswalk id**, so it lands in Tier 3 weighted agreement rather than a Tier 1
  adoption.
* **A wrong `team` is worse than no team.** `team` carries 0.20 of the
  resolution weight and scores as *disagreement*. The feed writes nicknames
  (`Packers`), which are mapped to canonical abbreviations; the 61 active rows
  carrying a slash-joined multi-club string resolve and publish with
  `team: null`, because the ordering is not consistent — `PHI/IND/WAS` (Wentz)
  is chronological while `IND/ATL` (Ryan) is reversed, so neither the first nor
  the last segment is reliably the current club.
* **An unmapped position 422s the whole batch.** `player-identity`'s
  `build_query` rejects a position outside `KNOWN_POSITIONS` and
  `/resolve/batch` validates the whole body, so one `ED` costs all 500 queries
  in its chunk. Six of OverTheCap's eighteen codes (`ED`, `IDL`, `LT`, `RT`,
  `LG`, `RG`) are absent from that vocabulary and cover 952 of the 2,908 active
  rows. `OTC_POSITIONS` maps them, and an unrecognised future code maps to
  `None` rather than being passed through.

`otc_player_id` is published on every row. It is OverTheCap's own stable key —
provenance, and a seed for a future `otc` crosswalk source. It is **not** an
`fdy-` id and must never be read as `player_id`; the schema pins `player_id` to
`^fdy-` so a swap fails the conformance test.

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
and **must not be reconciled backward**. With the cap fields null this is latent
rather than active — but it becomes live the moment a paid feed supplies them,
so nothing here reads a prior envelope back and no reconcile-backward path is
built "for later". The only prior state the process keeps is the in-memory
publish digest, which is forward-only and never rewrites an object.

## Cost, and why the loop is off

1.13 MiB per changed pass, one feed, conditional GET (the asset serves an `ETag`
and answers `If-None-Match` with a `304`). Cheap — cost alone would not settle
`CAPTURE_ENABLED`. Three things do:

1. **The document is frozen** (above). A daily poll of a 2022 snapshot buys
   nothing and writes 2022 contracts into an append-only lake.
2. **`broadcast-context`'s vacuous-guard exception does not apply.** Nothing
   here reads a prior lake snapshot back, so one dispatched `POST /refresh`
   produces exactly what a season-long loop would.
3. **It would be the third collector reaching a third party from its loop**
   (today only `weather` and `broadcast-context` do), and it fails closed on the
   scope — so on a cluster where `roster-scope` has not yet published, every
   loop pass would write a `present: 0` envelope and count a capture failure.

A dispatched `POST /refresh` reaches the upstream regardless of the flag.

## Tests

```bash
cd services/player-contract
uv run pytest -v
```

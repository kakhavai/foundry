# officiating

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8021` |
| Gateway path | `/collectors/officiating` |
| Cadence class | `weekly` |
| Signal types | `game_crew_assignment`, `crew_tendency_rates` |
| Capture loop | **off** (`CAPTURE_ENABLED=false`) — see [Cost](#cost) |

## What it captures

Who is calling each game, and what that group has historically done to it. The
fantasy-relevant channel is not fairness but volume: crews differ in penalties
called per game, and defensive pass interference in particular converts an
incompletion into a large chunk of yardage with no receiver credited. A crew
that stops the clock more often also runs more total plays, which lifts every
skill player in the game.

Three free upstreams, all nflverse, all measured live on 2026-08-01:

| Feed | Size | Carries |
|---|---|---|
| `games.csv` | 0.49 MiB on the wire (2.07 MiB parsed) | the schedule, and the `old_game_id` crosswalk |
| `officials.csv` | 1.23 MiB | who worked each game, with `official_id` |
| `play_by_play_<season>.csv.gz` | 18.2 MiB | penalties and snaps (93.4 MiB raw) |

## Two findings that changed the design

**The crosswalk is published; the spec assumed it was not.** The spec says the
penalty/crew join is "by hand-maintained crosswalk". `officials.csv` speaks the
legacy gamekey (`2025090400`) and play-by-play speaks the modern id
(`2025_01_DAL_PHI`) — but `games.csv` carries **`old_game_id`** holding exactly
the legacy value alongside the modern one. All 272 games of the 2025 regular
season join, none unmatched in either direction. No table is maintained here.

**`official_id` is present, which is what makes the crew-churn guard possible.**
Every 2025 row carries one. The spec's requirement to "resolve individual
officials, not just the referee's name" is therefore satisfiable rather than
aspirational — and it matters: `games.csv`'s own `referee` column disagrees
with `officials.csv` on **17 of 272** 2025 games. Sixteen are "Ron Torbert"
versus "Ronald Torbert" — one man, two display forms, which keyed by name is a
phantom eighteenth crew and keyed by `official_id` is nothing at all. The
seventeenth (`2025_13_NYG_NE`, "Alex Kemp" versus "Shawn Smith") is a real
disagreement between the feeds and survives the surname comparison.

**So `officiating_referee_disagreements` has a baseline of 1 on the 2025
season, not 0.** Alert on a rise; `> 0` pages on the first scrape.

## Fields that are null by necessity

`officials.csv` is a **post-game** record — it says who worked a game that has
been played, not who is scheduled to work one. Two spec fields therefore have
no free source and are emitted as `null` with `null_field_reason` set on every
row, rather than fabricated, defaulted to `false`, or dropped from the schema:

| Field | What would fill it |
|---|---|
| `assignment_announced_at` | a forward-looking assignment feed with a publication timestamp — Football Zebras publishes crews midweek, but scraped, not licensed |
| `is_provisional` | the same feed; a post-game record cannot be provisional |

The post-game nature also sets a trap for coverage, and the trap is the reason
`EXPECTED_FLOOR` is what it is. In week 3 the officials feed describes 48 games
of 272. Declaring the expectation from what it published would report
`expected: 48, present: 48`, ratio 1.0, all season, with most of the season
missing. The floor is 272 games and 17 crews, independent of any fetch.

## The two guards

The spec's named failure mode is *"rates that are pure sampling noise presented
as tendencies"*. Both answers are implemented, not noted, and they are separate
guards because they fail differently.

**Shrinkage + the refusal to serve** (`rates.py`). Every rate is regressed
toward the league mean by the random-effects weight `k = tau2 / (tau2 +
sigma2/n)`, ships with `games_sampled` and `rate_stderr = sqrt(sigma2/n)`, and
carries its own split-half correlation. A rate whose split-half correlation is
not distinguishable from zero **and** was not materially shrunk is refused:
`value` is `null`, `raw` survives, `refusal_reason` says why.

Measured against the real 2025 regular season, **not one of the seven per-game
rates is stable** — the best is `penalty_yards_per_game` at r = 0.365 with a
95% interval running from -0.140. Every one is also materially shrunk (k
between 0.000 and 0.571), so every one is served, as a shrunk estimate,
honestly labelled. That is the collector working: the product is the crew's
rate *plus* the statement that at sixteen games it is barely distinguishable
from average.

**Crew churn** (`crews.py`, alarmed in `capture.py`). `crew_continuity_pct` is
the share of an assignment's members also on that crew across the sampled
window. The alarm fires below 0.6 **and only when that crew's rates are being
served** — a crew whose rates were all refused is making no claim about anyone.
Against 2025 the median assignment sits at 0.955 and 5 of 272 fall below 0.6.

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health`
and `/metrics` requires `Authorization: Bearer <token>`.

Plus **`GET /crews/{crew_id}`** — the crew's rate profile and observed member
roster, independent of any scheduled game, so a consumer can evaluate a crew
before assignments publish. An unknown crew is 404; a malformed `crew_id` is
422. `crew_id` is `<season>-ref<referee official_id>`, e.g. `2025-ref693`.

`/signals` filters: `crew_id`, `game_id`, `referee_name` (plus the universal
`season`, `week`, `signal_type`). A `crew_tendency_rates` row has no `game_id`,
and filtering by one excludes it rather than treating the absent field as a
wildcard.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Cost

`CAPTURE_ENABLED=false`, deliberately. One pass reads 19.9 MiB across the three
feeds. That is four times what `player-identity` ships false for, and the Kind
cluster is rebuilt on every CI run.

All three feeds serve ETags and answer `If-None-Match` with a 304 carrying zero
bytes (verified live), so the steady state is nearly free — but the ETag store
is in memory, so every pod restart pays the full 19.9 MiB again. That is the
cost this flag controls.

Taking play-by-play gzipped rather than raw is the other half. Two measurements
over the real 2025 file decided it, and both went into
`collector_core.streaming` rather than this adapter because
`defense-vs-position` reads the same document:

| | bandwidth | CPU | peak memory |
|---|---|---|---|
| `.csv`, full rows | 93.4 MiB | — | flat |
| `.csv.gz`, full 372-column rows | 18.2 MiB | 9.8 s | flat |
| `.csv.gz`, 6 of 372 columns | 18.2 MiB | **3.8 s** | flat |

## Tests

```bash
cd services/officiating
uv run pytest -v
```

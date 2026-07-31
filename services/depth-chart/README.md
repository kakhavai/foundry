# depth-chart

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8006` |
| Gateway path | `/collectors/depth-chart` |
| Cadence class | `volatile` |
| Signal types | `team_depth_chart`, `depth_chart_stability` |
| Upstream | nflverse `depth_charts_{season}.csv` — **52.9 MB**, one asset per season |
| `CAPTURE_ENABLED` | `false` — a load decision, see below |

## What it captures

Who is ahead of whom at each position on each team, and separately **whether
that ordering can be believed**. The published depth chart and the functional
one disagree often enough that treating them as one field would be an error: a
team may list a veteran as the starter while the rookie takes 70% of the routes.
Carrying the ordering, plus how long it has held and how fresh the chart behind
it is, lets the generator weight the chart by its own reliability instead of
trusting it flatly.

Two signal types, split by **grain**:

- `team_depth_chart` — one row per charted player. The ordering itself, plus
  `weeks_at_rank` (a per-player fact).
- `depth_chart_stability` — one row per `(team, position)` group.
  `rank_changes_4w`, `stability_score`, and the freshness pair
  `chart_age_days` / `is_stale` (per-group facts).

The upstream feed is a **time series of snapshots**, not one snapshot: the same
player appears once per `dt` going back over the season. That is what makes the
stability half computable inside a single pass, with no lake read.

## The failure mode this collector is built around

**A stale chart reads as maximum confidence.** If a vendor freezes a preseason
chart and keeps serving it, `weeks_at_rank` climbs every week and
`stability_score` converges on 1.0 — the collector reports its strongest
possible signal precisely when the data is dead, and nothing in the row looks
wrong. `stability_score` cannot detect this, because a genuinely settled room
produces the identical number.

`chart_published_at` is therefore taken from the upstream's own `dt` and never
from the fetch instant, `chart_age_days` / `is_stale` are carried as an
independent axis, and `depth_chart_stale_charts` is the Prometheus gauge to
alert on. Cross-check a high `stability_score` against `is_stale` before
believing it.

## Coverage

`coverage.expected` is **160** — 32 teams x 5 configured positions
(`QB, RB, WR, TE, K`), declared in [`depth_chart/universe.py`](depth_chart/universe.py)
before any upstream is contacted. Coverage counts **position groups, not
players**: a team publishing a chart with a position group omitted registers as
missing, and a chart listing six receivers where the config expects a group does
not register as more coverage than one listing four.

## Fields that are deliberately null

Each is a refusal to invent, not an omission. See `build_chart_signal` in
[`depth_chart/capture.py`](depth_chart/capture.py) for the full reasoning.

| Field | Why null | What would fix it |
|---|---|---|
| `player_id` | `player-identity` mints canonical ids anchored on its own upstream's record key; there is no offline derivation from this feed | A shared player-identity client in `collector-core`. `gsis_id` is carried meanwhile and joins to `external_ids.gsis` |
| `functional_rank`, `disagreement` | The spec assigns these to an in-repo computation over `usage-share` | `usage-share` shipping |
| `is_starter` | "Opened the most recent game" is a participation fact, not a charting one. `official_rank == 1` is a different claim, already carried | `usage-share` shipping |
| `role_label` (beyond `qb1`/`qb2`) | The feed distinguishes three receiver lanes numerically but never names them; mapping lane 1 to `wr_x` would be a coin flip | `usage-share`'s alignment rates |
| `roster_status` | Reconciled against `roster-transactions`, which is not registered. This feed carries no status column | `roster-transactions` shipping |

## Routes

The standard five from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh` — plus the spec's

    GET /signals/diff?from=<captured_at>&to=<captured_at>

which reports ordering changes between two captures (`added`, `removed`,
`promoted`, `demoted`) so a consumer can react to a promotion without diffing
full snapshots itself. `404` when either capture does not exist: "nothing
changed" and "that capture never happened" are different answers.

Everything except `/health` and `/metrics` requires
`Authorization: Bearer <token>`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Why `CAPTURE_ENABLED=false`

A **load** decision, the same one `player-identity` and `roster-scope` made —
never a way to dodge a startup bug. The upstream asset is 52.9 MB and the
cadence class is `volatile` (every 15 minutes); left on, every CI cluster and
every pod restart would pull several GB a day from a free community mirror this
project does not pay for. `POST /refresh` reaches the upstream regardless of the
flag, which is how the path is exercised deliberately rather than continuously.

## Memory

The adapter never holds the response twice, filters to the five configured
positions as it parses, and retains at most `MAX_WEEKS` ISO-week buckets per
group. Peak memory is therefore flat in the size of the feed —
`roster-scope` was `OOMKilled` at this same 256Mi limit reading this same
upstream. Do not raise the limit; measure instead.

## Tests

```bash
cd services/depth-chart
uv run pytest -v
```

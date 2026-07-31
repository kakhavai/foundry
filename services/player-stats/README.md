# player-stats

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8004` |
| Gateway path | `/collectors/player-stats` |
| Cadence class | `weekly` |
| Signal types | `player_box_weekly` |
| Depends on | `player-identity`, `roster-scope` (both seams — see below) |
| Status | **Live**, deployed with `CAPTURE_ENABLED=false` |

## What it captures

What a player actually produced in a completed game, expressed once in raw
counting stats and once as fantasy points under each scoring format the
platform serves. No other collector carries realized production: every
backward-looking feature the generator builds — trailing averages, boom/bust
variance, format-sensitivity — resolves to rows from here.

It is deliberately the **only collector permitted to compute fantasy points**,
so the scoring rules live in exactly one place (`scoring.py`). The upstream
publishes its own `fantasy_points` columns and they are ignored on purpose:
adopting them would move the scoring rules into whichever feed happened to be
wired up.

Upstream: nflverse weekly player stats, `stats_player_week_{season}.csv`
(~8.3 MB, 145 columns, one row per player per week of the whole season). It is
streamed and filtered to the scoped week as it parses — never buffered.

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.

`GET /signals` filters on `season`, `week`, `signal_type` (universal) plus
`player_id`, `game_id`, `team` and `position`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

### `GET /revisions` — this collector's one extra route

```
GET /revisions?since=2026-09-16T00:00:00Z[&season=2026&week=1]
-> {"season": …, "week": …, "since": …, "count": …,
    "revisions": [{"game_id": …, "player_id": …,
                   "revision": …, "captured_at": …}]}
```

Restated `(game_id, player_id, revision)` tuples, so the generator can
invalidate cached features without re-reading the whole week. `season`/`week`
default to the scope the capture loop runs on. A `since` that is not RFC 3339
is a **422**, never a silently ignored filter — returning the whole history
would read as "nothing has been restated in my window".

## Where `revision` comes from

The candidate upstream publishes no revision marker, so it is **derived**
against the previous lake snapshot (`revisions.py`):

| | |
|---|---|
| unchanged counting stats | the same revision as last pass |
| changed counting stats | last pass's revision + 1 |
| never seen before | revision 0 |

which makes it monotonic per `(game_id, player_id)` by construction. `rates`
and `fantasy_points` are deliberately outside the fingerprint — they are
derived from the counting stats, so including them would make a scoring-table
change read as the whole league being restated.

If the previous snapshot cannot be read, the pass **fails** rather than
continuing: minting revision 0 over rows already at revision 3 would break the
monotonicity every consumer pins against.

## `coverage.expected`

> One row per `roster-scope` watchlist player whose team has completed its
> game for the scoped week, plus any non-watchlist player who recorded at
> least one offensive snap in those games.

`EXPECTED_FLOOR` is **384** — `32 teams x (2 QB + 3 RB + 4 WR + 2 TE + 1 K)`,
taken from `roster-scope`'s config rather than from anything fetched.
`roster-scope`'s own universe is 416; the 32 team-defense slots have no box
score and can never be owed a row here.

A row that is ambiguous emits nothing and is counted in `coverage.missing`
with a reason. The one that matters is `team_game_mismatch`: a row whose
`team` is in neither half of its own `game_id` is an upstream that back-filled
the player's *current* club onto a game they played for someone else.

"At least one offensive snap" is approximated, conservatively, by at least one
pass attempt, carry or target — this feed carries no snap count. A non-watchlist
player who was on the field but never involved is therefore invisible until
`usage-share` lands.

## Three fields the upstream cannot supply

| Field | Emitted | Why |
|---|---|---|
| `offense_snaps` | `null` | Snap counts are in the participation feed, which is `usage-share`'s upstream. A fabricated `0` would read as "dressed but never played" |
| `stat_state` | `null` | The spec requires exposing the upstream's *own* certification level "rather than inferring it from elapsed time". nflverse publishes none. **Do not** replace this with a Monday-to-Wednesday heuristic |
| `played` | always `true` | The feed cannot see a dressed player who never took a snap, so this collector never emits `played: false` — a watchlist player with no row is a `coverage.missing` entry instead |

## The two platform seams

**The roster-scope watchlist is read from the lake, not over HTTP, and has no
on/off switch.** `adapters/scope.py`'s `fetch_watchlist` reads the last
`roster-scope` capture straight out of the lake on every pass — decision 5 of
the narrowing design — so a `roster-scope` outage costs this collector's
coverage *freshness*, never `/signals`'s availability. It raises
`ScopeUnavailable` rather than returning an empty set, and `capture.py` fails
the whole pass closed on that: no scope means zero calls to the box-score
feed and a `present: 0` envelope, never an unnarrowed capture.

**`PLAYER_IDENTITY_URL` is empty in `helm/values/player-stats/values.yaml`.**
That is this collector's own crosswalk seam (`adapters/identity.py`), separate
from the watchlist above: while it is unset, `build_id_resolver` falls back to
a deterministic stub that hashes the upstream GSIS id. Both `roster-scope` and
this collector now resolve real ids through `player-identity` when pointed at
it, so the two collectors' canonical ids agree once both are configured — the
stub-vs-stub mismatch that used to keep the watchlist unreliable no longer
applies.

## Metrics beyond the fleet-wide set

- `player_stats_restatements_total` — rows whose counting stats changed
  against the previous snapshot. The spec's own alert: a spike outside the
  Monday-to-Wednesday window usually means an adapter is re-emitting unchanged
  rows as new revisions, and every one of those is a *present* row, so
  coverage stays at 1.0 while the lake fills with fictional corrections.
- `player_stats_identity_misses` — rows whose upstream id did not cross-walk
  to a canonical `player_id`. The phase doc calls this the failure most likely
  to go unnoticed for a season.

## Tests

```bash
cd services/player-stats
uv run pytest -v
```

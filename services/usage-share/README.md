# usage-share

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8005` |
| Gateway path | `/collectors/usage-share` |
| Cadence class | `weekly` |
| Signal types | `player_usage_weekly` |
| Upstream | nflverse weekly player stats, one CSV per season (~8.3 MB) |
| Status | **Live, partial fields** — no snaps, no routes; see below |

## What it captures

What the offense *gave* a player, independent of what they converted it into.
Opportunity is the more stable half of production — target share and route
participation persist week to week where yards and touchdowns do not — so a
backup who inherits 80% of the routes is projectable before a single box score
reflects it.

This is also the only collector that carries the **team-level denominators**,
without which every share is uninterpretable. Every share it publishes is
computed here from an explicit base travelling in the same row, never taken
from whatever the vendor already divided. That is the whole defence against
this collector's headline failure mode: a share computed against the wrong
denominator looks entirely normal — 0.71 is plausible whether the base was 62
real snaps or 68 including kneels and special teams — and is enough to reorder
a depth chart.

## What this upstream cannot supply, stated rather than hidden

The feed carries every opportunity numerator (targets, receiving air yards,
carries) and the pass volume `team_dropbacks` is built from. It carries **no
offensive snap counts and no routes**. So:

| Field | Value today | Why |
|---|---|---|
| `snap_share` | `null` | Its base is not available, and *"a share arriving without its base is rejected rather than stored"* |
| `route_participation` | `null` | Same; the spec explicitly permits a partial row rather than blocking the week |
| `denominators.team_offense_snaps` | `null` | Not `0` — "we cannot see snaps" is a different fact from "nobody took any" |
| `redzone`, `goal_line`, `two_minute`, `alignment` | objects of `null` | Situational splits need play-by-play. Present rather than absent, so a consumer never has to tell "not supplied" from "key missing" |
| `usage_source` | `derived` | Never `charted`: every share is inferred from counting stats |
| `player_id_source` | `upstream_gsis` | See below |

`player_id_source` is **not** in the phase doc's field table and is added
deliberately. That table documents `player_id` as *"the canonical id from
`player-identity`"*, and this collector cannot produce one: roster-scope's
membership rows carry no name and no external id, so there is nothing to join a
GSIS-keyed feed onto. Emitting the upstream id under a field documented as
canonical, with no way for a consumer to tell, is exactly the silent gap the
envelope's coverage block exists to prevent. The field flips to
`player_identity` when the join exists.

## `coverage.expected`

`32 teams × (11 offensive-skill scope slots + 1 denominators object) = 384`,
declared as a constant and **never derived from the document just fetched**.

The 11 is roster-scope's config quota (`QB≤2, RB≤3, WR≤4, TE≤2`); its full
universe is 13 per team, but the extra two are one kicker and one team defense,
and neither records offensive usage. The `+1` is the team's own `denominators`
object, which the spec names as part of complete coverage in its own right.

- total outage → `0 / 384`, ratio **0.00**
- truncated document → `100 / 384`, ratio **0.26**
- a healthy week observes *more* than 384 keys, because this collector captures
  every offensive-skill player the feed carries rather than only the ~352 in
  scope. The floor raises a short count and never lowers a genuine one.

## Scope awareness

The registry says `scope_aware: false`, deliberately differing from the phase
doc's plan line. See the comment on the entry in
`contracts/collector-registry.yaml`: narrowing to roster-scope's watchlist is
not implementable until roster-scope publishes a join key, and capturing the
superset also removes the spec's own second failure mode for this collector
(scope drift shortening a trailing average).

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.

**No extra routes**, per the spec. `GET /signals` accepts `season`, `week`,
`signal_type` (universal) plus `player_id`, `team` and `game_id`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Metrics beyond the fleet-wide set

- `usage_share_team_sum_drift` — the largest distance from 1.0 that any team's
  target shares summed to, measured against the **upstream's own** share column.
  Independent by construction: the shares this collector computes are divided by
  a base it summed itself and add to 1.0 trivially. Alert above `0.03`.
- `usage_share_invalid_shares` — rows refused for a share outside `[0, 1]`,
  labelled by which one.
- `usage_share_rows_captured` — recorded every pass, including zero.

## Tests

```bash
cd services/usage-share
uv run pytest -v
```

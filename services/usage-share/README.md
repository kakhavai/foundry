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
| `player_id_source` | `player_identity` | See below |

`player_id_source` is **not** in the phase doc's field table and is added
deliberately. That table documents `player_id` as *"the canonical id from
`player-identity`"*, and it now is one — see the next section. The field is
kept rather than dropped because the lake is append-only: every row this
collector wrote before the join existed still says `upstream_gsis` and always
will, and without the field those rows would be indistinguishable from these.

## Narrowing — the roster scope, joined forward

There are ~1,700 players in the league and ~416 that matter. This feed carries
every one of the ~1,700; this collector publishes only the offensive-skill
players named in `roster-scope`'s membership list.

The scope is read from the **lake**, never from `roster-scope` over HTTP — the
lake is append-only and already holds every scope capture, so a `roster-scope`
outage costs scope *freshness* rather than this collector's availability.

**The join runs forward.** The scope is a set of canonical `fdy-` ids and the
feed is keyed by GSIS id, so it could have run in either direction. Each
upstream row's `gsis_id` is resolved through `player-identity` to an `fdy-` id
and then checked against the scope, because `gsis` is a *published crosswalk
source* — `player-identity` adopts the link at Tier 1, exactly, with no
attribute scoring at all. The reverse direction (416 `fdy-` ids back to GSIS
ids) has no seam today and is left to 8C.

No name is sent with the query. A GSIS id absent from the crosswalk would
otherwise fall through to attribute scoring, and a feed that already carries a
league id and is matched by name anyway is how two Josh Allens become one
player. A miss stays a miss and the row is dropped.

**`player-identity` is authoritative.** Anything not explicitly `resolved: true`
is unresolved and the row is dropped — never published under the upstream's id,
and never under a `candidates` entry the service declined to adopt.

**It fails closed, and that costs zero upstream calls.** Both seams are resolved
*before* the CSV is touched. No scope, no `player-identity` to resolve against,
or a lake that cannot be read at all, and the pass writes a `present: 0`
envelope and fetches nothing. The `errors` array names which: `scope_unavailable`
(nothing published for this week or the last), `scope_empty` (this week's scope
resolved zero members), `identity_unavailable` (`PLAYER_IDENTITY_URL` is empty),
or `malformed`/`unknown` (the lake read itself failed). There is deliberately
**no unnarrowed fallback** — one would blow the vendor's request budget
precisely during an incident.

`identity_unavailable` is the *config* case only, and that distinction matters
because the other one does not fail the pass at all. If `PLAYER_IDENTITY_URL`
**is** set but `player-identity` cannot be reached — a 401, a connection
refusal, a timeout — `IdentityClient.resolve_many` returns a partial result and
records the reason out of band rather than raising, so one dead chunk cannot
discard the chunks that resolved. The capture therefore runs to completion with
those rows dropped, and files one summarised `identity_upstream_error` entry
naming how many rows were lost and to what. Without it an outage would report
nothing but `below_expected_floor` — the same envelope a two-member scope or a
truncated feed produces, and three incidents with three different fixes should
not share one symptom. One entry per pass, never one per row, so a total outage
cannot push every other reason past the 50-entry cap.

`PLAYER_IDENTITY_URL` is therefore set in `helm/values/usage-share/values.yaml`
rather than shipping empty as it does for `player-stats` and `injury-report`.
An empty value here is not "run unnarrowed", it is "resolve nothing, narrow to
nothing, publish nothing".

## `coverage.expected`

`32 teams × (11 offensive-skill scope slots + 1 denominators object) = 384`,
declared as a constant and **never derived from the document just fetched**.

The 11 is roster-scope's config quota (`QB≤2, RB≤3, WR≤4, TE≤2`); its full
universe is 13 per team, but the extra two are one kicker and one team defense,
and neither records offensive usage. The `+1` is the team's own `denominators`
object, which the spec names as part of complete coverage in its own right.

- total outage → `0 / 384`, ratio **0.00**
- truncated document → `100 / 384`, ratio **0.26**
- a real week runs **just under** the floor: 2024 week 1 measured `375 / 384`,
  ratio **0.977** — measured *before* narrowing, so treat it as the right order
  of magnitude rather than a current benchmark — because a handful of watchlist
  slots legitimately record no
  stat line at all and nflverse omits the row. That is honest coverage, not a
  fault — but it does mean `errors` carries `below_expected_floor` on a normal
  week. **Alert on the ratio, not on the presence of that entry**: truncation
  reads ~0.26 and an outage reads 0.00, so the two are nowhere near each other.

The floor raises a short count and never lowers a genuine one, so a week where
more than 384 keys are observed still reports honestly.

## Scope awareness

This collector narrows — see [Narrowing](#narrowing--the-roster-scope-joined-forward)
above. The registry entry's `scope_aware` flag, and the platform gate that makes
the flag falsifiable rather than merely type-checked, land with the final task
of the scope-narrowing plan.

The earlier claim that narrowing here was *"impossible, because roster-scope's
membership rows carry no name and no external id"* assumed the **reverse** join
and predates `collector_core.identity`. It is not true and is not the reason for
anything.

What narrowing does give up is real and worth stating: capturing the superset
removed the spec's own second failure mode for this collector — scope drift,
where a player promoted into the watchlist in week 9 has no rows for weeks 1–8
and any trailing average silently computes over a shorter denominator. That
failure is now live again, and it belongs to whoever consumes trailing windows.

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

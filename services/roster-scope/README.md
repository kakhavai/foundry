# roster-scope

The collector that answers "which players is Foundry currently paying
attention to, and why". It is the only place that answer exists: every other
collector fetches `GET /scope/players` first and only pulls signals for the
players on it, so widening the universe to `WR≤5` is one edit in
`roster_scope/rules.py` rather than 24 edits across the fleet.

| | |
|---|---|
| Port | 8003 |
| Signal types | `scope_membership_weekly`, `scope_change_event` |
| Cadence class | `weekly` |
| Stage | 8A |
| Depends on | `player-identity` — conceptually, not in code (see below) |

## Layout

```
roster_scope/
  main.py                 descriptor + the three extra routes (<60 lines)
  capture.py              ledger -> charts -> resolution -> envelopes -> lake
  rules.py                CONFIG: teams, quotas, overrides, expected_slots()
  scope.py                RESOLUTION: ordering, grace, versions, route views
  metrics.py              the shared fleet metrics plus this collector's two
  telemetry.py            OTel wiring, imported only behind the env guard
  adapters/
    depth_chart.py        upstream ordered-player feed
    identity.py           the player-identity seam
```

Two adapters and two domain modules is more than the minimum collector shape,
and both are load-bearing: there really are two upstreams, and config
(`rules.py`) versus resolution (`scope.py`) is the split that keeps
`coverage.expected` honest.

## `coverage.expected` is 416, and it is config-derived

32 teams × (QB 2 + RB 3 + WR 4 + TE 2 + K 1 + DST 1) = **416**.

The slot keys are built from `rules.py` alone, *before* a chart is fetched.
This is the single most important property of the service. The Phase 8 spec
names the failure it prevents: because coverage would otherwise be computed
from the same resolved list that drives fetching, a stale depth chart that
omits the correct player would produce 100% coverage across the entire
downstream fleet — every collector reporting full coverage for the wrong ~350
players.

Consequences worth knowing:

- A team whose chart yields three receivers under `WR≤4` contributes one
  `coverage.missing` entry.
- An unresolvable name is a **missing slot**, never a skipped row.
- A total chart outage yields `present: 32` (the config-derived team defenses)
  out of 416 — a ratio of ~0.077, and an envelope that is still written, with
  a populated `errors` array.

## The `player-identity` seam

`PLAYER_IDENTITY_URL` empty (the default, and what the values file ships)
selects `StubPlayerIdentityResolver`: a deterministic
`fdy-<sha256(normalized_name|team|position)[:12]>`. It **refuses rather than
guesses** — blank team or position, or a name normalizing under two
characters, raises `UnresolvablePlayer` and the slot is recorded missing.

Setting `PLAYER_IDENTITY_URL` selects `HttpPlayerIdentityResolver`. Its
request/response shape is **PROVISIONAL** and has not been agreed with the
`player-identity` author — read the class docstring before setting the
variable in any values file.

## Local

```bash
cd services/roster-scope && uv run pytest -v

# built from the repo root: collector-core is a path workspace member
docker build -f services/roster-scope/Dockerfile -t roster-scope:local .
```

# schedule-context

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8009` |
| Gateway path | `/collectors/schedule-context` |
| Cadence class | `weekly` |
| Signal types | `game_situational_context`, `team_rest_context` |
| Upstream | nflverse game table (`SCHEDULE_URL`), ~2.1 MB, streamed |
| Status | **Live**, deployed with `CAPTURE_ENABLED=false` — see below |

## What it captures

What the calendar is doing to a club before the ball is snapped: how many
hours since it last played, how far it flew, how many time zones it crossed,
and whether this is the third road game in a row. These effects are team-wide
and modest per game, but they are entirely absent from a player's own history,
and they are the main reason a Thursday performance systematically differs
from the same club's Sunday performance.

One record per club per game — **two records per game** — split across two
signal types:

- **`team_rest_context`** — rest and bye adjacency. Derived from kickoff
  timestamps and from the weeks a club is actually scheduled in. Needs no
  venue, so it survives an upstream this collector cannot fully place.
- **`game_situational_context`** — travel, body clock, acclimatisation and
  road stretch. Needs the venue's coordinates and IANA zone.

The split is load-bearing: a game at an unrecognised neutral-site stadium
costs a *situational* row and nothing else, rather than costing everything
this collector knows about that club's week.

## Two things that would be silently wrong

**`days_rest` is computed from kickoff timestamps, never from calendar
dates.** The upstream ships its own `away_rest` / `home_rest` columns and this
collector ignores them, because they are whole-day integers derived from
dates. A Monday-night 20:15 game followed by a Saturday 13:00 game is 4.70
days of rest; date subtraction reports 5, and `is_short_week` comes out False
when it should be True.

**The venue is never guessed.** The feed's `stadium_id` on a neutral-site row
describes the *designated home club's* stadium, not where the game is played.
Trusting it fetches Detroit's coordinates for a game in Munich — plausible
numbers, schema-valid, wrong by four thousand miles. Neutral rows resolve by
stadium name, and an unrecognised name resolves to nothing and is counted in
`coverage.missing` with `venue_unresolved`.

## Coverage

`coverage.expected` is derived from the league's structure, never from the
document the fetch returned:

| Weeks | Floor | Why |
|---|---|---|
| 1–4, 15–18 | 32 | No byes: every club plays, two records per game |
| 5–14 | 26 | The bye window; at most six clubs rest in one week |
| 19–22 | 12, 8, 4, 2 | The postseason bracket is a constant |

That week-awareness is what stops a bye week and an outage looking alike. A
week with four clubs on bye observes 28 records against a floor of 26 and
reports 1.0; the same week captured during an outage observes 0 against 26 and
reports 0.0. Since `Coverage.ratio` reads 1.0 for `0/0`, a floor of zero would
make the second case indistinguishable from a healthy empty week.

Rows that were owed but could not be produced are counted with a reason:
`kickoff_unscheduled`, `venue_unresolved`, `rest_unresolved`.

## Known gap

`schedule_revision_count` is always `null`. The upstream is a single snapshot
with no memory of where a kickoff used to be, so any number would be invented.
Deriving it from this collector's own append-only lake is a real option and a
deliberate follow-up: it needs a bounded prefix read on every pass.

`venues.py` is transitional. When the `venue` collector (8E) lands, it replaces
that table behind the same `Venue` interface — a config change, not a rewrite,
the same arrangement `weather`'s bundled schedule adapter is in today.

## Deployment

`CAPTURE_ENABLED=false` in `helm/values/schedule-context/values.yaml`. This is
a load decision, not a workaround: the upstream is a 2.1 MB third-party
document, the Kind cluster is recreated on every CI run, and every pod restart
would re-fetch it. `POST /refresh` reaches the upstream regardless of the flag.

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.
There are no routes beyond the five, so there is no `smoke.sh`.

`GET /signals` accepts `season`, `week`, `signal_type` (applied by the shared
router) plus `game_id`, `team` and `opponent`. Note `team` filters the row's
`team_id`; anything else is a 422 rather than a silently ignored filter.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Tests

```bash
cd services/schedule-context
uv run pytest -v
```

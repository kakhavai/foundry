# injury-report

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8007` |
| Gateway path | `/collectors/injury-report` |
| Cadence class | `volatile` |
| Signal types | `player_injury_status`, `team_injury_report` |
| Scope-aware | **no**, on purpose — see below |
| Status | **Stub upstreams** — `INJURY_REPORT_URL` and `SCHEDULE_URL` ship empty |

## What it captures

Whether a player will be available, and — more usefully — the week-long
trajectory that predicts it. A `questionable` tag preceded by DNP/DNP/Limited
means something quite different from one preceded by Limited/Full/Full, so a
row carries the whole week's `practice_participation` and `designation_history`
rather than only the latest value.

It is the only collector that distinguishes **"no designation was published"**
(`game_designation: null`) from **"published as carrying no designation"**
(`game_designation: "none"`), and the only one that distinguishes a club that
filed a report listing nobody from a club that filed nothing at all. Those two
distinctions are the collector; everything else here is in service of them.

### It deliberately ignores the roster scope

Every other signal collector narrows to `roster-scope`'s membership list before
it fetches. This one does not, and `scope_aware: false` in
`contracts/collector-registry.yaml` is a decision rather than a default falling
through. An opposing cornerback ruled out moves a receiver's projection as much
as the receiver's own hamstring does, and defenders never appear on an
offence-oriented watchlist at all. Narrowing here would silently discard the
half of the signal that is hardest to get anywhere else.

`GET /signals?team=…` is still available as a convenience. It is not a
boundary, and a consumer that uses it has thrown away the opponent side.

## The two upstreams, and why there are two

| Module | Answers | Why separate |
|---|---|---|
| `adapters/schedule.py` | which clubs **owe** a filing this week, and for which game | It is the coverage denominator. Read out of the injury feed instead, a truncated feed would report `3 of 3`, ratio 1.0, while twenty-nine clubs' reports vanished. |
| `adapters/upstream.py` | which clubs **filed**, on which practice day, and what | The only module that knows the wire format, including the vocabulary mapping. |

Only the disagreement between the two is interesting, and it is only visible
because they are two documents.

`adapters/identity.py` is the `player-identity` seam. `PLAYER_IDENTITY_URL`
ships empty and ids are minted deterministically from the upstream's own stable
player key; a row with no such key is dropped and counted rather than hashed
from a display name.

## Empty report vs. no report

The distinction is structural, so nothing has to remember to check it:

| upstream | `team_injury_report` row | coverage |
|---|---|---|
| club filed, players listed | `filing_status: "published"` | present |
| club filed, listed nobody | `filing_status: "empty"` | **present** |
| club filed nothing | *no row at all* | **missing**, reason `report_not_published` |

A club that filed appears in `signals` either way, so a consumer reading only
the rows sees the difference. A club that did not file appears in
`coverage.missing`, so a consumer reading only coverage sees it too. Neither
channel can express "healthy" for a club that said nothing.

## What `coverage.expected` counts

One filing per club with a scheduled game, **per practice day elapsed** — the
phase doc's wording, unchanged. Both factors come from outside the injury feed:
the clubs from the schedule upstream, the days from the clock
(`report.practice_days_elapsed`, since the report week runs Wednesday to
Tuesday).

`EXPECTED_FLOOR` is the third guard: **26** clubs per practice day, being
thirty-two minus the six that are ever on a bye in one week. It never lowers a
genuine count, so a real expansion past thirty-two still reports honestly.

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.
The phase doc gives this collector no extra routes, so there are none.

`GET /signals` accepts `team`, `player_id`, `game_id`, `practice_day` and
`game_designation` beyond the universal three. `POST /refresh` returns
**202 — accepted, not done**; the capture runs as a background task, so poll
`/signals` rather than reading it on the next line.

## Its own metrics

Beyond the fleet-wide `collector_*` series:

| Metric | Catches |
|---|---|
| `injury_report_teams_published{practice_day}` / `injury_report_teams_with_games{practice_day}` | one club's feed breaking on **one day**, which a week-level ratio hides |
| `injury_report_unmapped_rows{reason}` | understanding less of the feed every week, which otherwise looks like the feed getting quieter |

## Before the real upstream is wired

1. Set `INJURY_REPORT_URL` (with `{season}`/`{week}` placeholders) and
   `SCHEDULE_URL` in `helm/values/injury-report/values.yaml`.
2. Reconsider `CAPTURE_ENABLED` in the same change. It is `"true"` today
   because stub mode reaches no third party; a 15-minute poll against a real
   feed is a different decision.
3. Check the wire's vocabulary against `adapters/upstream.py`'s alias tables.
   Anything unrecognised drops the row with a reason — loudly, by design — so a
   feed whose spellings differ will show up as `injury_report_unmapped_rows`
   rather than as bad data.

## Tests

```bash
cd services/injury-report
uv run pytest -v
```

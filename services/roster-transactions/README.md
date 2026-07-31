# roster-transactions

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8008` |
| Gateway path | `/collectors/roster-transactions` |
| Cadence class | `volatile` |
| Signal types | `roster_transaction` |
| Depends on | `player-identity` (for the canonical `player_id`) |
| Status | **Stub upstream** — see `adapters/upstream.py` |

## What it captures

Answers *what changed between two depth-chart snapshots, and when it took
effect*. A depth chart is a photograph; without the transaction wire it goes
stale the moment a starter lands on IR or a practice-squad receiver is elevated,
and the staleness is invisible because the snapshot still parses cleanly. This
is also the only place the platform records **eligibility** — a player signed
Thursday who cannot appear until week 6, an IR return window that opens in 21
days — which availability alone cannot express.

Two fields carry most of that weight. `announced_at` and `effective_at` are kept
separate because the gap between them is exactly the window in which every depth
chart in the platform is wrong. And `transaction_id` is derived from the move's
own content rather than passed through from the upstream, because the same move
arrives under different ids on different feeds and under a *new* id on the same
feed when it is upgraded from `reported` to `official`.

## Coverage counts time, not transactions

This collector's `coverage.expected` is the one that differs from the rest of
the fleet, and the reasoning lives in [`windows.py`](roster_transactions/windows.py).
Every other collector counts things — 32 teams, 416 scope slots, ~2,900 rostered
players. An event stream has no such number: a quiet Tuesday legitimately has
zero transactions, so `expected = len(rows)` would report a feed that returned
**nothing** as ratio 1.0.

So `expected` is the set of 15-minute intervals of the scoped week that have
fully elapsed as of `now` — derived from the calendar and the clock, two things
the upstream cannot influence — and `present` is the subset the upstream's
manifest **acknowledged** covering. A quiet week with a healthy feed is 1.0. A
failed poll during that same quiet week is not.

## Routes

The standard five, from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health` and
`/metrics` requires `Authorization: Bearer <token>`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

Plus one beyond the five:

`GET /events?since=<cursor>&limit=<n>` — cursor-paged event stream, because this
collector is event-shaped rather than snapshot-shaped and a consumer wants
"everything since the last thing I saw" rather than a whole week re-read. The
order is `(announced_at, transaction_id)`; the tiebreak is not optional, since a
trade is two rows at the same instant. `next_cursor` is `null` only when the
stream is exhausted. Paging is over the **currently scoped week's** cache — a
consumer crossing a week boundary advances `season`/`week` rather than expecting
the cursor to carry it.

`/signals` accepts `player_id`, `team`, `transaction_type` and `confidence` on
top of the universal three. `team` matches **either side** of a move: matching
only `to_team` would hide every player a team lost, which is the half that
breaks a depth chart.

## Before this is real

The upstream is still stubbed, and it is the only thing left:

1. Point `adapters/upstream.py`'s `UPSTREAM_URL` at the real feed and delete the
   placeholder branch and `PLACEHOLDER_ROWS`. The adapter expects a small JSON
   **manifest** (`covers_from`, `covers_through`, `feed_url`) plus a streamed CSV
   feed; the manifest is where the acknowledgement the coverage block turns on
   comes from, and it cannot be inferred from the rows.
2. Map the vendor's transaction vocabulary onto the closed 13-value enum in
   `transactions.py`. Refuse anything unrecognised rather than bucketing it —
   `roster_transactions_unknown_types` is the series that makes a vendor rename
   visible.
3. Decide `CAPTURE_ENABLED` in `helm/values/roster-transactions/values.yaml`.
   It ships `false`; a dispatched `POST /refresh` reaches the upstream
   regardless of that flag.

`EXPECTED_FLOOR`, `build_signal` and `metrics.py` are done — see the coverage
section above and `metrics.py`'s docstring for why each series exists.

## Tests

```bash
cd services/roster-transactions
uv run pytest -v
```

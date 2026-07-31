# usage-share

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8005` |
| Gateway path | `/collectors/usage-share` |
| Cadence class | `weekly` |
| Signal types | `player_usage_weekly` |
| Status | **Stub upstream** — see `adapters/upstream.py` |

## What it captures

TODO: one paragraph. What raw signal is this, where does it come
from, and what makes it worth a collector of its own?

## Routes

The standard five, from `collector_core.routes`: `GET /health`,
`GET /metrics`, `GET /catalog`, `GET /signals`, `POST /refresh`. Everything
except `/health` and `/metrics` requires `Authorization: Bearer <token>`.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Before this is real

1. Point `adapters/upstream.py`'s `UPSTREAM_URL` at the real feed and delete
   the placeholder branch.
2. Set `EXPECTED_FLOOR` in `capture.py` to the size this collector's universe
   is actually known to have. It must not be derived from a fetch.
3. Rewrite `build_signal` and mirror its output in
   `contracts/signal-envelope/collectors/usage-share.json`.
4. Replace the placeholder series in `metrics.py`, or drop the subclass.
5. Decide `CAPTURE_ENABLED` in `helm/values/usage-share/values.yaml`.

## Tests

```bash
cd services/usage-share
uv run pytest -v
```

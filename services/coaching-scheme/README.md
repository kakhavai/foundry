# coaching-scheme

A Foundry signal collector. Scaffolded by `scripts/new-collector.py`; see
[`docs/collectors.md`](../../docs/collectors.md) for the authoring guide.

| | |
|---|---|
| Port | `8023` |
| Gateway path | `/collectors/coaching-scheme` |
| Cadence class | `seasonal` |
| Signal types | `staff_assignment`, `team_scheme_profile` |
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
   `contracts/signal-envelope/collectors/coaching-scheme.json`. **This one is
   enforced.** The generated schema and the generated `build_signal` agree
   with each other by construction, so the conformance test proves they match
   rather than that either is right. Each signal type therefore carries a
   `$comment` marker, and `tests/test_placeholder_schemas.py` fails on any
   collector that reaches the repo still carrying it — or still carrying the
   placeholder's `key`/`observed_at`/`value` field set with the marker
   deleted.
4. Replace the placeholder series in `metrics.py`, or drop the subclass.
5. Decide `CAPTURE_ENABLED` in `helm/values/coaching-scheme/values.yaml`.

## Tests

```bash
cd services/coaching-scheme
uv run pytest -v
```

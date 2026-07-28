# Testing Strategy

What is tested at each layer, why, and where it runs.

## Layers

| Layer | Scope | Mocks | Runs in |
|---|---|---|---|
| Unit | One function or module | `respx` stubs the HTTP boundary | per-service `test` job |
| Property | Parser robustness under generated input | `respx` | per-service `test` job |
| Contract | Payload shape, API surface stability | none — schemas and snapshots | per-service `test` job |
| Integration | Whole app over real HTTP | only the external upstream | per-service `test` job |
| Helm render | Chart output correctness | none — real `helm template` | `platform-tests` job (root `tests/`) |
| Smoke | Deployed services in a Kind cluster | none | `integration-test` job |

## Coverage

80% line **and branch**, enforced by `--cov-fail-under=80` in each package's
`pyproject.toml`. **No files are excluded from measurement.** Excluding a file
to reach a number is hiding the gap the gate exists to surface — `telemetry.py`
sat at 0% in every service until Phase 5A and is now covered by tests that
assert wiring rather than SDK internals.

Coverage is reported to the GitHub Actions job summary on every run, including
failed ones.

## What Each Layer Catches

**Unit** — logic errors in a single unit, with collaborators stubbed.

**Property (Hypothesis)** — the failure class hand-written tests miss: missing
fields, wrong types, non-object bodies, oversized payloads. These found two real
defects in `player-projections` during Phase 5A: an untyped `AttributeError` on
a non-object snapshot, and a single malformed record discarding an entire batch.

**Contract** — that the shape other systems depend on has not changed silently.
The OpenAPI snapshot test is the automated catch for "response field renamed",
a fault type in the Phase 5C catalog. Intentional API changes regenerate the
snapshot in the same PR, making every surface change explicit in review.

**Integration** — that the wired-together app behaves correctly over HTTP:
concurrency, upstream timeouts, malformed upstream responses, per-item
degradation.

**Helm render** — cross-file consistency that no service test can see. The OTel
collector DNS name is the standing example: wrong value, traces and logs stop
silently while `/metrics` keeps working. This lives in the repo-root `tests/`
directory, which nothing executed before Phase 5A; a `platform-tests` job was
added to `.github/workflows/integration-test.yml` to run it, and is now a
required status check alongside `integration-test`.

**Smoke** — that the deployed thing actually serves traffic in a cluster.

## Adding Tests for a New Service

1. Copy the `pyproject.toml` coverage block from `services/weather`.
2. Write unit tests with `respx` for the upstream boundary.
3. Add a property suite for any parser handling external data.
4. Commit an OpenAPI snapshot to `contracts/openapi/<service>.json` and add the
   divergence test.
5. Add an integration suite under `tests/integration/`.
6. Confirm the 80% gate passes before opening the PR.

## Known Limits of These Tests

Stated plainly so nobody mistakes a green suite for a proof it does not carry.

**The OpenAPI snapshot cannot see response field names.** Handlers return bare
dicts with no `response_model=`, so FastAPI emits an empty schema for every 200
response. The snapshot catches routes added or removed, path-parameter changes,
and operationId renames — not a renamed field inside a body. That is what the
response-shape contracts exist for.

**Response-shape contracts pin a list by its first element only.** `player-projections`
is a bulk-only API — `GET /projections` returns the full in-memory list, there
is no per-player lookup route. The seed leads with a skill player, so `rank`,
`proj_points.*`, and `blurb` are contracted while DST's `yahoo_rank`/`espn_rank`
are not — those are asserted directly in the integration suite instead. A `null`
value or an empty list or dict also collapses its subtree to a bare path, which
is why fixtures must be generated against a populated, successful response.

**The `player-data` schema encodes an intended shape, not an observed one.**
No provider exists yet. It becomes genuinely enforcing when `player-data`
validates its own output against the same file in its CI. One schema gap
remains, not three: `floor <= expected <= ceiling` cannot be expressed in JSON
Schema 2020-12 — there is no portable way to compare sibling properties — so it
is asserted as a business rule against the **committed fixtures only**
(`test_fixture_spreads_are_ordered`), not against arbitrary provider output.
`format: date-time` is mechanically enforced (`jsonschema[format]` with a
`FormatChecker` attached — see `test_bad_generated_at_is_rejected`), and the
`pos`-based conditional positively excludes the wrong branch's fields in both
directions (see `test_dst_row_carrying_skill_player_fields_is_rejected` and
`test_wr_row_carrying_dst_fields_is_rejected`) — a DST row carrying `rank` now
fails validation, as does a skill player carrying `yahoo_rank`. `FLEX` is a
frontend-derived display lane, not a stored position, and does not appear in
the `pos` enum.

**Concurrency tests are regression guards, not race detectors.** `weather` holds
no shared mutable state, so its concurrent test cannot currently fail for the
reason its name suggests. `player-projections` does hold shared state in
`main._state`, but no writer runs during the read burst, so under asyncio's
cooperative scheduling it is close to guaranteed to pass. Both are worth keeping
— they start doing real work the moment shared state or a concurrent writer
appears — but neither proves race safety today.

**Coverage is a floor, not evidence of good tests.** Every package sits above
80%, and `weather` is at 100%. That measures execution, not assertion quality.
Several tests in this phase reached the reviewer as passing-but-toothless and
had to be strengthened; assume the next one will too.

**`_poll_loop`'s bare `except Exception` emits no signal.** Every upstream
failure in `player-projections` — 5xx, timeout, malformed snapshot, encoding
error — collapses to one boolean (`upstream_healthy`), with no log, no metric,
no exception detail, and no staleness bound on the retained cache. The service
can serve week-old data indefinitely and nothing in Loki, Tempo, or Prometheus
says why or for how long. This is deliberate for now: neither service logs
anything today, and choosing a platform-wide logging approach (structured vs
plain, OTel log bridge or not) is its own decision rather than a side effect of
a testing phase. Tracked as a Phase 5B follow-up.

**The coverage gate is 80 against actuals of 93-100.** A regression from 100%
to 81% passes silently. Deliberate: a ratcheting threshold makes unrelated PRs
fail. Tracked as a Phase 5B follow-up, not raised here.

## Not Covered Here

Chaos scenarios, load and scale testing, and adversarial agent sessions are
Phase 5B and 5C. See `docs/architecture/phase-5-resilience-and-ai-testing.md`.

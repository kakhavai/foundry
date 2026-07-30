# Contracts

Four directories, split on two axes: **direction** (what we consume vs what we
serve) and **authorship** (hand-written vs generated from code) — plus one
file, `collector-registry.yaml`, which is neither and is described at the
bottom.

| Directory | Direction | Written by | Covers |
|---|---|---|---|
| `projections-snapshot/` | inbound | a human | the S3 documents the projections generator publishes |
| `projections-api/` | outbound | a human | types and constraints on `GET /projections` |
| `openapi/` | outbound | `app.openapi()` | route/param/status-code surface per service |
| `responses/` | outbound | a test helper | response body field *names*, at any depth |

## Why four and not fewer

`openapi/` and `responses/` are two halves of one job. FastAPI handlers here
return bare dicts with no `response_model=`, so the generated OpenAPI emits
`"schema": {}` for every 200 body — it can prove a route exists but says nothing
about what comes back. `responses/` fills that by recording dotted field paths
(`projections[].proj_points.ceiling`) and diffing them.

`response_model=` would collapse the two, and was rejected deliberately: it
changes runtime behaviour by filtering undeclared fields out of live responses.
A contract file cannot break production; a response model can.

`projections-api/` then adds what neither generated artifact carries — **types**.
A row whose `rank` arrives as `"three"` instead of `3` passes the OpenAPI
snapshot and passes the response-shape contract, because the field names are
unchanged. Only the schema catches it.

## The two directions fail differently

**Generated** (`openapi/`, `responses/`): a failure means the code changed.
Either fix the code, or regenerate and commit the file in the same PR so the
surface change is explicit in review. Regeneration commands are in the failure
messages the tests print.

**Hand-written** (`projections-snapshot/`, `projections-api/`): these are never
stale, because no machine writes them. A failure means the schema or the fixture
is wrong, and you edit it yourself.

`projections-snapshot/` is design-first — its producer runs outside this
repository and does not exist yet, so the schema is a statement of intent rather
than an observation. `projections-api/` is design-first for the same reason: the
frontend that will consume it has not been built.

## Cross-file references

`projections-api/response.v1.schema.json` `$ref`s the player definition out of
`projections-snapshot/snapshot.v1.schema.json` by its `$id`, so a player row has
exactly one definition across both directions. The `$id` host
(`foundry.internal`) does not resolve over the network on purpose — tests map it
to the file on disk with a `referencing.Registry`.

## `collector-registry.yaml` — the odd one out

Hand-written like the two schema directories, but it is not a schema and it
describes neither an inbound nor an outbound payload. It is an **inventory**:
which collectors exist, where the gateway routes them, what they emit, and what
they depend on. The projections generator reads it to decide what to call, and
the phase-8 doc has the gateway serving it live at `GET /collectors`.

Its own shape is pinned by `collector-registry.schema.json` beside it. What
makes it different from everything else here is the third source of truth:
`projections-snapshot/` describes a producer nobody in this repo can observe,
so nothing can check it. The registry describes services that are right here,
so it **can** be checked, and it is —
`tests/test_collector_registry.py` compares every entry against the service's
own `CollectorDescriptor` (read by AST, not imported), its Helm values, and its
GitOps manifests; `scripts/check-registry.py` compares it against each
collector's live `GET /catalog` from inside `scripts/smoke-test.sh`.

Two things it does **not** prove, stated here so a green run is not
over-read:

- `scope_aware` is type-checked as a bool and nothing more. There is no
  representation of scope-awareness in code today, so its correctness is
  human-reviewed at PR time.
- The registry lists **deployed** collectors, not planned ones. The staging
  table in `docs/architecture/phase-8-data-source-collectors.md` is the plan.
  A green registry says nothing about the twenty-odd collectors still unbuilt.

## Caveat on the word "snapshot"

It means two different things here. `projections-snapshot/` uses the domain
sense: the weekly point-in-time dump of projections. `openapi/` and `responses/`
hold *snapshot tests* in the testing sense: committed copies of generated output,
diffed on every run. Same word, opposite roles.

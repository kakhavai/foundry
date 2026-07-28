# Projections snapshot contracts

The projections generator — which runs outside this repository — publishes one
JSON document per scoring format per week to S3.
`player-projections` polls the document matching the requested format.

**One schema — `snapshot.v1.schema.json` — covers all three formats.** Scoring
changes the *values* of `rank` and `proj_points` for skill positions, never the
shape of the document, so there is nothing for a per-format schema to say. The
document's own `format` field records which of the three it is:

| `format` | Scoring |
|---|---|
| `standard` | Standard (no reception points) |
| `half-ppr` | 0.5 points per reception |
| `ppr` | 1 point per reception |

This started as three near-identical files whose shapes were kept in sync by a
test. Collapsing them makes that invariant structural rather than asserted.
The tradeoff: a per-file `format: {"const": "ppr"}` could catch the PPR
document being published at the standard URL, and an enum cannot. That check
now lives at read time in the consumer instead — `fetch_projections(url,
expect_format=...)` rejects a document whose `format` is not the one being
polled. It catches the real misconfiguration in production rather than only
in CI.

Each player row is one of two shapes, selected by `pos`:

- **DST** carries two ratings instead of a single projection: `yahoo_rank`
  and `espn_rank`. It has no `rank` or `proj_points`.
- **Every other stored position** (`QB`, `RB`, `WR`, `TE`, `K`) carries
  `rank` and `proj_points` (a `floor`/`expected`/`ceiling` spread). The
  intended invariant is `floor <= expected <= ceiling`; JSON Schema 2020-12
  cannot compare sibling properties, so this is not mechanically enforced by
  the schema — assert it in provider and consumer tests instead.

`blurb` is an optional short free-text field (a few sentences) that the
frontend shows on hover to explain a player's rating.

`FLEX` is **not** a stored position and does not appear in the `pos` enum.
It is a frontend-derived display lane, populated client-side from RB/WR/TE
rows — there is nothing for the schema to represent.

Kicker (`K`) uses standard kicking scoring, so it is format-independent:
its rows are identical across `standard`, `half-ppr`, and `ppr`. `DST` rows
are likewise identical across all three files, since they carry rank data
rather than a scoring-dependent projection. This duplication (~64 rows) is
deliberate — it means the frontend makes one fetch per scoring mode and has
everything it needs, with no second file and no merge step.

## Direction

These contracts are **provider-driven**: the schema is authoritative and both
sides conform to it. See [ADR 0002](../../docs/adr/0002-provider-driven-contracts.md)
for why this project does not use consumer-driven contract testing (Pact), and
the condition under which that decision should be revisited.

## When the generator is built

It must validate its output against these files in its own CI before
publishing. Until then the schemas are validated against the fixtures in
`fixtures/` only — they encode an intended shape, not an observed one.

## Versioning

The `.v1.` in the filename is the contract version. A backward-compatible
addition (a new optional field) may amend v1 in place. Any change that would
break an existing consumer — removing a field, narrowing a type, changing an
enum — requires a new `.v2.` file published alongside v1.

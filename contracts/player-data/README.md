# player-data snapshot contracts

`player-data` publishes one JSON document per scoring format per week to S3.
`player-projections` polls the document matching the requested format.

| File | Scoring |
|---|---|
| `standard.v1.schema.json` | Standard (no reception points) |
| `half-ppr.v1.schema.json` | 0.5 points per reception |
| `ppr.v1.schema.json` | 1 point per reception |

The three schemas are structurally identical. Scoring format changes the
*values* of `rank` and `proj_points`, not the shape of the document.

## Direction

These contracts are **provider-driven**: the schema is authoritative and both
sides conform to it. See [ADR 0002](../../docs/adr/0002-provider-driven-contracts.md)
for why this project does not use consumer-driven contract testing (Pact), and
the condition under which that decision should be revisited.

## When `player-data` is built

It must validate its output against these files in its own CI before
publishing. Until then the schemas are validated against the fixtures in
`fixtures/` only — they encode an intended shape, not an observed one.

## Versioning

The `.v1.` in each filename is the contract version. A backward-compatible
addition (a new optional field) may amend v1 in place. Any change that would
break an existing consumer — removing a field, narrowing a type, changing an
enum — requires a new `.v2.` file published alongside v1.

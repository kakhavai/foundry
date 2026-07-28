# ADR 0002 — Provider-Driven Contract Testing

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** Platform owner (kakhavai)
- **Context:** Phase 5A — Rigorous Service & Platform Testing

---

## Context and Problem Statement

Phase 5A introduces contract testing. Two hops need contracts:

| Hop | Shape | State |
|---|---|---|
| projections generator → `player-projections` | S3 JSON document, polled | Producer is out of repo |
| `player-projections` → `fantasy-frontend` | HTTP request/response | Consumer not built |

The reference spec named Pact. Pact implements *consumer-driven* contract
testing: each consumer declares what it needs, and those expectations are
replayed in the provider's CI.

## Decision

Enforce contracts **provider-side**, with committed schemas:

- **JSON Schema** for the projections snapshot documents — one schema covering
  all three scoring formats, in `contracts/projections-snapshot/`.
- **Committed OpenAPI snapshots** for `weather` and `player-projections`, in
  `contracts/openapi/`, with CI failing on undeclared divergence.

Do not adopt Pact.

## Rationale

**Foundry is a monorepo and owns both sides of every hop.** Pact's purpose is
coordinating independently deployed services across separate repos and CI
systems. When consumer and provider land in the same CI run, the real consumer
can be tested against the real provider — strictly better than a pact, which is
a recorded approximation of that interaction.

**Neither hop currently has both sides.** The generator lives outside this repo;
`fantasy-frontend` does not exist. Pact today would be half-inert either way.

**The upstream hop is a document, not request/response.** Pact covers this via
message pacts, but that is its least-exercised path in Python and it buys less
than a schema. JSON Schema is the native format for a document contract: any
future generator, in any language, validates against the same file with a
stock library.

## What This Gives Up

Provider-driven schemas are not consumer-driven. They do not record *which
fields the consumer actually reads*, so they cannot tell a future generator
author that renaming a field is safe because nobody consumes it. That is Pact's
genuine advantage and it is being given up deliberately. Keeping the schemas
minimal — only fields the consumer reads — partially approximates it.

## Revisit Trigger

Adopt Pact when **a consumer of a Foundry service lives outside this
repository's CI** — a partner integration, a separately deployed frontend, or a
service owned by another team. At that point the consumer can no longer be run
against the real provider in a single job, and consumer-driven contract testing
starts paying for itself.

## Alternatives Considered

**Adopt Pact now for demonstration value.** Foundry exists partly to
demonstrate platform practice, and Pact is a pattern worth knowing. Rejected:
shipping a tool that does not fit the topology demonstrates the tool, not the
judgment. This ADR demonstrates more.

**Defer contract testing entirely until both sides exist.** Rejected: the
OpenAPI snapshot tests are valuable *today* against services that exist now,
and catch a fault type already listed in the Phase 5C fault catalog.

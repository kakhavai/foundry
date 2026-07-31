# Scope narrowing: authoritative identity, a matchup scope, and a fleet that fetches only what matters

**Date:** 2026-07-30
**Status:** Approved, not yet implemented
**Affects:** `libs/collector-core`, `services/roster-scope`, `services/player-identity`
(config only), and the six Phase 8B collectors

---

## Why

Only a few hundred of roughly 1,700 rostered players matter to a fantasy
projection. Fetching all of them wastes vendor API budget, memory, and money.
`roster-scope` exists to publish that short list — ~416 slots — and every other
collector is supposed to narrow to it before pulling anything.

**Today no collector narrows at all.** `PLAYER_IDENTITY_URL` and
`ROSTER_SCOPE_URL` are `""` in every `helm/values/*/values.yaml`, so each
collector mints player ids from its own local stub and **the ids do not join**.
A collector cannot ask "which of my upstream's players are in the scope?"
because there is no shared key to ask it with.

Three collectors hit this wall independently during 8B and each documented it
rather than guessing: `depth-chart` ships `player_id` null, `usage-share` emits
raw `upstream_gsis` ids, `player-stats` shipped with both seams off. The
registry's `scope_aware` field therefore describes an intention that is
fleet-wide unrealized.

This design makes narrowing possible, then mandatory.

---

## Decisions

Five, all made deliberately and recorded here so the reasoning survives.

| # | Decision | Rejected alternative |
|---|---|---|
| 1 | **`player-identity` is authoritative.** Every collector resolves its upstream's native id through it and emits the canonical `fdy-` id. | Collectors emit native ids and the generator joins downstream — makes in-collector narrowing impossible, which is the whole point. |
| 2 | **A second, separately-bounded matchup scope**, rather than widening the single list. | Widening to ~930 slots would make every 8B collector fetch roughly twice what it needs. |
| 3 | **Role-matched matchup quotas** — only positions that bear on a scoped player's projection. | All 11 defensive starters + 5 OL: simpler, but includes positions no 8D collector asks about. |
| 4 | **Fail closed.** No scope means no fetch. | Falling back to an unnarrowed fetch would blow the API budget precisely during an incident. |
| 5 | **The scope is published to the lake; collectors read the last good one.** | Live HTTP to `roster-scope` plus fail-closed would make one service a fleet-wide stop. |

---

## Architecture

```
player-identity ──▶ roster-scope ──▶ [ the lake ] ──▶ every collector
   (fdy- ids)        (two lists)     (last good scope)  (narrow, then fetch)
```

**Collectors read the scope from the lake, not from `roster-scope` over HTTP.**
This is the least obvious choice in the design and the one that earns its
keep: the lake is already append-only, already written by `roster-scope`, and
already read by collectors, so it costs no new infrastructure — and it means a
`player-identity` outage degrades the *freshness of the scope* rather than
stopping all 26 collectors.

`roster-scope` still serves `GET /scope/players` and `GET /scope/matchups` over
HTTP. Those routes are for the out-of-repo projections generator and for
operators. Collectors do not use them.

---

## Components

### New in `collector-core`

Both land once. A collector writes neither.

**`ScopeClient`**

- Reads the most recent scope envelope from the lake for a given scope kind
  (`players` or `matchups`).
- Returns the member set and the envelope's `captured_at`.
- **Fails closed:** if no scope envelope exists, it raises. The caller must then
  write a `present: 0` envelope and make zero upstream calls.
- Never calls `roster-scope` over HTTP.

**`IdentityClient`**

- Resolves upstream native ids to canonical `fdy-` ids via
  `POST /resolve/batch` (≤500 per request — chunk above that).
- **Never re-ranks `candidates`.** Anything not explicitly `resolved: true` is
  unresolved, full stop. `player-identity` files the miss server-side; a caller
  that applies its own confidence floor adopts identities that service
  explicitly refused. That exact bug existed in `roster-scope` and was fixed —
  it must not return.
- Caches within a capture pass unconditionally.
- Caches across passes keyed on `player-identity`'s own `captured_at`, which the
  crosswalk response carries. Its cadence class is `seasonal`, so the crosswalk
  barely moves and this is safe.

### `roster-scope`

- **Wire identity:** `PLAYER_IDENTITY_URL` is set; the deterministic stub
  resolver is used only when it is empty (unchanged behaviour, now not the
  deployed default).
- **Matchup quotas in `rules.py`**, following the existing `DepthRule` shape so
  `expected_slots()` stays computable from config alone, before any upstream is
  contacted:

  | Position | Max per team | Why |
  |---|---|---|
  | `CB` | 4 | covers WR |
  | `S` | 3 | covers WR/TE deep |
  | `LB` | 3 | covers TE/RB |
  | `DL` | 4 | pressure, run defence — bears on RB and QB |
  | `OL` | 5 | our own line, versus that front |

  ~19 slots per team, ~608 total. Excludes special teams, practice squad, and
  backups beyond quota.

  **Two things the implementer must not gloss over.**

  First, `OL` is **our own** line, not the opponent's — it belongs in a matchup
  scope because pass protection bears on the QB and RB we already care about.
  Every other row in that table is an opponent. The list is therefore not
  "the opposing defence"; it is "the players who determine how our scoped
  players perform". Name and comment it that way or it will be misread.

  Second, **`POSITION_ALIASES` today covers offensive positions only**
  (`QB/RB/WR/TE/K` and their chart synonyms). Defensive labels are messier than
  offensive ones — `CB`/`DB`/`NB`, `S`/`FS`/`SS`, `LB`/`ILB`/`OLB`/`MLB`,
  `DL`/`DE`/`DT`/`NT`/`EDGE` — and vary more between sources. Extending that map
  is a required part of step 2, not an afterthought, and an unrecognised label
  must be **dropped and counted in `coverage.missing`**, exactly as the existing
  code does for unknown team labels. Guessing a position is worse than
  declaring the slot unfilled.

- **New signal type** `scope_matchup_weekly` and route `GET /scope/matchups`.
- **Coverage accounted separately per list.** A matchup resolution failure must
  not mask a healthy player scope, or vice versa. Two envelopes, two coverage
  blocks.

### Each Phase 8B collector

Read the scope from the lake → resolve upstream ids → narrow → fetch. Flip
`scope_aware: true` in the registry.

---

## Where narrowing applies

This differs by upstream shape and must be stated, or a bulk fetch will be
"optimised" in a way that saves nothing.

- **Bulk-file upstreams** (nflverse CSVs — most of 8B): one HTTP request
  regardless of scope. Narrow **as you parse**. The saving is **memory**, not
  requests — and it is real: a 36.8 MB document already `OOMKilled` a collector
  in CI at the 256Mi limit.
- **Per-player APIs** (all of 8C's betting and props upstreams): narrow
  **before** fetching. The saving is **request count**, ~416 instead of ~1,700.
  This is where the vendor bill actually changes.

---

## Error handling

| Condition | Behaviour |
|---|---|
| No scope envelope in the lake | `present: 0`, every slot in `coverage.missing` with reason `scope_unavailable`, **zero upstream calls**. `/signals` continues serving the last good capture from memory. |
| Scope envelope exists but is stale | Narrow with it. Record `scope_age_seconds` in the envelope so staleness is visible rather than inferred. |
| `player-identity` unreachable | Affected players resolve to nothing; each lands in `coverage.missing` with reason `identity_unresolved`. The pass still writes an envelope. |
| `resolved: false` for a player | Never becomes an id. Counts as missing with `player-identity`'s own reason. |
| Lake write fails after a good capture | Unchanged from current behaviour: `publish_capture` records the failure and returns the envelopes anyway. An object-store outage costs durability, not availability. |

There is no "fetch everything" fallback anywhere in this design. That is
deliberate: the failure mode it would prevent (a week of missing data, loudly
flagged) is strictly better than the one it would cause (a silent 4× vendor
bill during an incident).

---

## Testing

**The behavioural scope test is the point of this section.** Each retrofitted
collector gets a test asserting it requested **only** scoped players, using the
`respx` mocking already present in every suite. This checks behaviour rather
than two declarations agreeing, and it is what finally makes `scope_aware`
verifiable — closing the gap the registry comment currently admits to.

Also required, per collector:

- **Fail-closed test:** with no scope in the lake, assert **zero** upstream
  requests were made and the envelope reports `scope_unavailable`.
- **Stale-scope test:** an old scope envelope still narrows, and
  `scope_age_seconds` is populated.

In `collector-core`, against the fake collector:

- `IdentityClient` never adopts a `resolved: false` response, including when
  `candidates` are present and score highly. **This is the regression test for a
  bug that already happened once.**
- Batch chunking at the 500 boundary.
- `ScopeClient` raises rather than returning an empty set when no envelope
  exists — an empty scope and a missing scope must not be confusable.

**Mutation testing is mandatory** for every behaviour above. Pair every
`all(...)`/`any(...)` over a collection with an explicit length assertion;
`all([])` is `True`, and that exact shape let mutations survive three separate
agents during 8B.

---

## Implementation order

1. **`collector-core`** — `ScopeClient` and `IdentityClient`, with the fake
   collector proving both.
2. **`roster-scope`** — identity wiring, matchup quotas, `GET /scope/matchups`,
   separate coverage per list.
3. **Pilot retrofit: `usage-share`.** Chosen because it documented the joining
   blocker most precisely, so it is the clearest test of whether this design
   removes it.
4. **The remaining five 8B collectors**, in parallel, once the pilot proves the
   pattern.
5. **8C and 8D**, built narrowed from the start rather than retrofitted.

Steps 1 and 2 are sequential. Step 3 gates step 4 deliberately: if the pattern
is wrong, discovering it once is much cheaper than discovering it five times.

**This decomposes into three implementation plans, not one.** They are listed
together because the reasoning is shared, but they ship separately:

| Plan | Steps | Gate before the next |
|---|---|---|
| **A — the seams** | 1 + 2 | `roster-scope` publishes both lists to the lake, with identity live |
| **B — the pilot** | 3 | `usage-share` narrows, and its behavioural test proves it fetched only scoped players |
| **C — the fleet** | 4 | all six narrow; then 8C/8D are built narrowed from the start (step 5) |

Attempting A and B in one pass is the specific mistake to avoid: the pilot's
whole value is that it exercises the seams from outside, and it cannot do that
if it is being written by the same change that builds them.

---

## Consequences

- **Issue #79 resolves.** `usage-share` shipped `scope_aware: false` only
  because narrowing was unimplementable. Once ids join it becomes `true`; the
  disagreement with the phase doc was a symptom, not a decision.
- **Issue #78 becomes worth doing properly.** With real narrowing there is
  behaviour to verify, so the gate can assert what a collector *did* rather than
  compare two declarations.
- **Issue #83 is unblocked.** The three approximate identity resolvers can now
  be replaced by `IdentityClient`, because the semantic question they were
  blocked on — whose id space wins — is decided.
- **`roster-scope` gains a runtime dependency on `player-identity`.** Its
  registry `depends_on` already declares it; this makes the declaration true.
- **8D is unblocked.** Its four collectors have a bounded list to narrow against
  instead of fetching every defender in the league.

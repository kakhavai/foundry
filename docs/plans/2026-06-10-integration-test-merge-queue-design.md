# Integration Test as a Real Merge Gate via GitHub Merge Queue

**Date:** 2026-06-10
**Status:** Approved design — ready for implementation plan

## Problem

The `mainb` ruleset on `main` requires one status check: `integration-test`. The
`integration-test.yml` workflow triggers on every PR, but its single job is guarded
by `if: contains(github.event.pull_request.labels.*.name, 'ready-for-merge')`.

When the label is absent the job is **skipped**, and **GitHub counts a skipped
required status check as a pass**. The net effect: every unlabeled PR satisfies the
required check without the integration test ever running, so PRs merge with no
integration coverage.

CLAUDE.md's "How CI Works" section claims the missing label *blocks* merge. It does
not. That false belief is what kept this hole hidden.

We want two properties:

1. **Auto-pass** when a PR changes nothing the integration test exercises (docs, CI
   config) — no ceremony required.
2. **Genuinely block** when a PR changes the deployable surface, until the
   integration test has actually run and passed.

## Why the label approach is the wrong tool

The integration test is expensive (it spins up a full Kind cluster, deploys the
observability stack and services, runs the smoke test). Running it on every PR push
is wasteful, which is why the label existed — to defer the cost to "merge intent."

But a label is a **manual, forgettable proxy** for merge intent. Forgetting it is
silent and high-cost (the test is skipped, not blocked). That is precisely the
failure mode we hit. Labels are also mutable by anyone with write access and are not
a natural readiness signal.

## Chosen approach: GitHub Merge Queue

A merge queue **is** merge intent, expressed natively. When a PR is marked "Merge
when ready," GitHub builds a temporary combined ref on top of the latest `main`,
fires the `merge_group` event, runs the required checks against that ref, and merges
only if they pass. No label, no human step to forget.

Merge queue is available at no cost here because the repository is **public**
(verified: `visibility: public`). The `mainb` ruleset requires **only**
`integration-test` (verified via `gh api`), so enabling merge queue does not risk
deadlocking on other required checks — there are none.

### The key insight: "skipped = pass" flips from bug to feature

The bug exists because a skipped required check counts as a pass. In the merge-queue
model, **every case where the job skips is a case where we genuinely want a pass**, so
the same mechanism now works *for* us. No aggregation/`gate` job is needed.

### Workflow design — `integration-test.yml`

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]   # 'labeled' trigger removed
  merge_group:                                # NEW — fires inside the queue

jobs:
  changes:
    if: github.event_name == 'merge_group'
    runs-on: ubuntu-latest
    outputs:
      relevant: <true if services/**, helm/**, infra/**, or scripts/** changed>

  integration-test:
    needs: changes
    if: github.event_name == 'merge_group' && needs.changes.outputs.relevant == 'true'
    runs-on: ubuntu-latest
    container: ghcr.io/kakhavai/foundry/ci-runner:latest
    # ... existing heavy Kind steps, unchanged ...
    # NOTE: the 'Remove label on failure' step is DELETED (no label anymore)
```

### Behavior matrix

The required check is `integration-test` (unchanged name — no ruleset rename).

| Context                          | `integration-test` job        | Required check    | Outcome                          |
|----------------------------------|-------------------------------|-------------------|----------------------------------|
| PR, any push                     | skipped (event ≠ merge_group) | skipped = pass    | PR is queueable, no heavy compute |
| In queue, docs/CI-only change    | skipped (not relevant)        | skipped = pass    | merges fast, no Kind spin-up      |
| In queue, deployable change      | **runs for real**             | actual result     | merges only if green              |

- On a PR, both `changes` and `integration-test` are skipped, so the only cost is job
  scheduling. The skipped `integration-test` still reports its check context, so the
  PR is queueable (not stuck "Expected").
- Inside the queue, `changes` computes relevance by diffing the exact change set being
  merged (`merge_group.base_sha..merge_group.head_sha`). Docs-only queued PRs skip the
  heavy job and merge immediately; deployable changes run the full test and merge only
  on success.

### Relevant-path scope

The integration test is required for changes under:

- `services/**`
- `helm/**`
- `infra/**`
- `scripts/**`

Rationale: the test runs `scripts/deploy-local.py` and `scripts/smoke-test.sh`
directly and deploys `infra/grafana-stack`, so `scripts/**` and `infra/**` are not
optional. `.github/**` and docs are deliberately excluded — gating CI-config changes
behind a full Kind run is high-friction and low-value (CI YAML is human-reviewed, and
the test effectively validates itself when it does run). The test-affecting CI
artifact `infra/ci/Dockerfile` is already covered under `infra/**`.

### Ruleset change (the one configuration step)

Add a `merge_queue` rule to the `mainb` ruleset:

- Merge method: **squash** (matches the existing `allowed_merge_methods: ["squash"]`).
- Keep `integration-test` as the required status check — no rename.

This is a production branch-protection change. It will be applied via `gh api` (or the
GitHub UI) and **confirmed with the user before execution**. It is reversible (remove
the rule).

## What gets removed

- The `ready-for-merge` label mechanism entirely:
  - the `labeled` trigger type,
  - the `if: contains(... 'ready-for-merge')` job guard,
  - the "Remove label on failure" `github-script` step.
- CLAUDE.md "How CI Works" rewritten: the integration test is now a true merge gate
  enforced by the merge queue; the label is gone.

## Out of scope / unaffected

- **Per-service workflows** (`weather`, `player-projections`): their `lint`/`test`/
  `helm-lint` jobs are **not** required checks, so leaving them PR-only causes no queue
  deadlock. They simply do not re-run inside the queue, which is acceptable.
- **`update-gitops-tag` jobs**: push to `main` via a bypass-actor GitHub App
  (`actor_id` exempt in the ruleset). Unchanged and already exempt from protection.

## Risks and rollout verification

Merge-queue behavior only exists on real PRs against `main`; it cannot be fully
exercised by local UAT. After merge, verify with a throwaway PR:

1. Open a **docs-only** PR → confirm `integration-test` shows green (skipped) and the
   PR is queueable → queue it → confirm it merges **without** a Kind spin-up.
2. Open a PR touching `services/**` → queue it → confirm the queue **runs the heavy
   integration test** and that the PR merges only after it passes.
3. Confirm that a deliberately failing integration test in the queue **prevents** the
   merge (PR is ejected from the queue).

The single assumption to validate in step 1 is that a skipped required check permits
queue entry — expected per GitHub's "skipped = success" semantics, but confirmed
empirically during rollout.

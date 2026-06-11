# Integration Test as a Path-Filtered Merge Gate

**Date:** 2026-06-10
**Status:** Final — supersedes the merge-queue design below

**Supersedes:**
- [`2026-06-10-integration-test-merge-queue-design.md`](2026-06-10-integration-test-merge-queue-design.md)
- [`2026-06-10-integration-test-merge-queue.md`](2026-06-10-integration-test-merge-queue.md)

## Problem (unchanged)

The required `integration-test` check was gated by `if: contains(labels, 'ready-for-merge')`.
A **skipped required check counts as a pass in GitHub**, so any PR without the label
satisfied the gate without running the test. PRs merged with no integration coverage.

## Decision trail (why this is the third design)

We went through two heavier designs before landing here. Recording why, because the
rejections are the actual lesson:

1. **Label + gate job.** Keep the `ready-for-merge` label as the "run it now" signal,
   and fix the skipped=pass hole with an always-runs `gate` job that fails when
   deployable paths changed but the label is absent. Rejected: the label is the
   *source* of the complexity. It is manual and forgettable (the exact failure that
   hid the original bug), and the gate job exists only to babysit it. Remove the label
   and the gate job becomes unnecessary.

2. **GitHub merge queue.** Trigger the heavy test on the `merge_group` event so it runs
   only at merge time. Rejected on two grounds:
   - **Wrong tool.** A merge queue exists to serialize and batch merges for repos with
     merge contention, testing each PR against the up-to-date base. It defers the test
     only as a side effect. A single-maintainer repo has no contention to serialize.
   - **Friction.** Enabling the `merge_queue` ruleset rule via `gh api` was rejected
     with `422 Invalid rule 'merge_queue'`. Not worth fighting for a property we can
     get more simply.

## Chosen approach: path filter, no label, no queue, no gate job

Stop trying to *defer* the test; instead only run it when it is *relevant*.

### Insight

The whole reason the label design needed a gate job was the "relevant change but not
labeled → must block" case. Without a label, that case does not exist: a relevant
change *always* runs the test. The only remaining skip case is "nothing relevant
changed," which we *want* to pass. So **skipped = pass works in our favor**, and no
aggregation/gate job is needed.

### Workflow — `integration-test.yml`

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]

concurrency:
  group: integration-test-${{ github.ref }}
  cancel-in-progress: true

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      relevant: ${{ steps.filter.outputs.relevant }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            relevant:
              - 'services/**'
              - 'helm/**'
              - 'infra/**'
              - 'scripts/**'

  integration-test:
    needs: changes
    if: needs.changes.outputs.relevant == 'true'
    runs-on: ubuntu-latest
    container: ghcr.io/kakhavai/foundry/ci-runner:latest   # (+ privileged docker mount)
    # ... existing heavy Kind steps, unchanged ...
```

### Behavior — `integration-test` is the required check

| PR change set            | `integration-test` job | Required check  | Outcome                |
|--------------------------|------------------------|-----------------|------------------------|
| docs / CI-config only    | skipped (not relevant) | skipped = pass  | merges, no Kind run    |
| deployable surface       | **runs for real**      | actual result   | merges only if green   |

`dorny/paths-filter@v3` is used (not a manual `git diff`) because it natively resolves
the PR base for `pull_request` events — no `fetch-depth: 0` or SHA juggling.

### Concurrency

`concurrency: { group: integration-test-${{ github.ref }}, cancel-in-progress: true }`.
`github.ref` is unique per PR (`refs/pull/<n>/merge`), so a new push to a PR cancels
*that PR's* prior in-flight run — never another PR's — instead of running a second Kind
cluster in parallel.

The two per-service workflows (`weather`, `player-projections`) get the same treatment,
but with `cancel-in-progress: ${{ github.event_name == 'pull_request' }}` so that PR
runs cancel while **main pushes serialize instead of cancelling** — an in-flight
`build-push` / `update-gitops-tag` deploy must not be killed mid-commit.

## Ruleset

**No change required.** The `mainb` ruleset already requires the `integration-test`
context, which this design keeps. (The `merge_queue` rule from the abandoned design
never applied, so the ruleset is already clean — verified: 4 rules, no `merge_queue`.)

## What is removed vs. the merged merge-queue version

- The `merge_group` trigger and the `merge_group`-only `if` guards.
- The `merge_group.base_sha..head_sha` manual diff (replaced by `dorny/paths-filter`).
- Docs corrected again: CLAUDE.md "How CI Works", `docs/deployment-lifecycle.md`,
  `docs/why-this-design.md` — all three described the merge queue and now describe the
  path filter.

## Note on current `main`

Before this change, `main` ran the integration test on *nothing*: the merged
merge-queue workflow only fired on `merge_group`, which never happens without a queue
enabled. This change restores real gating on every deployable-surface PR.

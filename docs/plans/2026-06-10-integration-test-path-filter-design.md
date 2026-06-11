# Integration Test as a Path-Filtered Merge Gate

**Date:** 2026-06-10
**Status:** Final

## Problem

The required `integration-test` check was gated by `if: contains(labels, 'ready-for-merge')`.
A **skipped required check counts as a pass in GitHub**, so any PR without the label
satisfied the gate without running the test. PRs merged with no integration coverage.

## Chosen approach: path filter, no label, no gate job

Stop trying to *defer* the test until someone signals "ready"; instead run it whenever
it is *relevant*. A `changes` job inspects the PR diff and the heavy test runs only when
the deployable surface (`services/`, `helm/`, `infra/`, `scripts/`) changed.

### Why not keep the label?

A label is the source of the complexity, not the cure. It is manual and forgettable —
the exact failure that hid the original bug — and defending against a forgotten label
requires an extra always-runs gate job whose only purpose is to babysit it.

Drop the label and that machinery disappears. Without a label, a relevant change
*always* runs the test, so the only remaining skip case is "nothing relevant changed,"
which we *want* to pass. **Skipped = pass now works in our favor**, and no gate job is
needed — `integration-test` itself stays the required check.

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
    # ... heavy Kind steps (create cluster, deploy stack, smoke test, tear down) ...
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
context, which this design keeps.

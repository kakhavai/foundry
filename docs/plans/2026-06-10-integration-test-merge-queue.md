# Integration Test Merge-Queue Gate Implementation Plan

> **⚠️ SUPERSEDED (2026-06-10)** by the path-filter design
> ([`2026-06-10-integration-test-path-filter-design.md`](2026-06-10-integration-test-path-filter-design.md)).
> The merge-queue approach was implemented and merged (PR #27) but then abandoned
> before enabling the queue — see the superseding doc for the rationale. Kept for the
> decision trail. Do not implement this.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken `ready-for-merge` label gate on the `integration-test` check with a GitHub merge queue, so the integration test genuinely blocks merges of deployable-surface changes and auto-passes everything else.

**Architecture:** The required `integration-test` check is rewired so it skips (= passes) on PRs and runs for real only on the `merge_group` event, gated by a path-relevance check. A merge_queue rule is added to the `mainb` ruleset. Because a skipped required check counts as a pass, every skip case is one we *want* to pass — so no aggregation/gate job is needed.

**Tech Stack:** GitHub Actions (`pull_request` + `merge_group` events), GitHub repository rulesets (`gh api`), bash, Kind/Helm/Helmfile (existing heavy test, unchanged).

**Design reference:** `docs/plans/2026-06-10-integration-test-merge-queue-design.md`

**Sequencing constraint (critical):** Tasks 1–2 (workflow + docs) ship in this branch and merge to `main` FIRST. Task 3 (enable merge queue) and Task 4 (rollout verification) are **post-merge operational steps** — enabling the queue before the `merge_group`-aware workflow is on `main` would deadlock queued PRs on an `integration-test` check that no workflow reports.

---

## File Structure

- `.github/workflows/integration-test.yml` — **rewritten.** Two jobs: `changes` (relevance detection, merge_group only) and `integration-test` (heavy Kind test, merge_group + relevant only). Label machinery removed.
- `CLAUDE.md` — **modified.** "How CI Works" section rewritten to describe the merge queue; the false "label blocks merge" claim removed.
- `mainb` ruleset (GitHub-side, via `gh api`) — **modified post-merge.** Adds a `merge_queue` rule (squash), keeps `integration-test` as the required check.

---

## Task 1: Rewrite the integration-test workflow

**Files:**
- Modify (full rewrite): `.github/workflows/integration-test.yml`

- [ ] **Step 1: Replace the entire workflow file with the merge-queue version**

Overwrite `.github/workflows/integration-test.yml` with exactly:

```yaml
name: integration-test

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'

on:
  pull_request:
    types: [opened, synchronize, reopened]
  merge_group:

jobs:
  # Relevance detection runs only inside the merge queue. On a PR this job is
  # skipped, which makes the dependent integration-test job skip too — a skipped
  # required check counts as a pass, so the PR stays queueable with zero compute.
  changes:
    if: github.event_name == 'merge_group'
    runs-on: ubuntu-latest
    outputs:
      relevant: ${{ steps.filter.outputs.relevant }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - id: filter
        name: Detect deployable-surface changes
        run: |
          BASE='${{ github.event.merge_group.base_sha }}'
          HEAD='${{ github.event.merge_group.head_sha }}'
          echo "Comparing $BASE..$HEAD"
          CHANGED="$(git diff --name-only "$BASE" "$HEAD")"
          echo "Changed files:"
          echo "$CHANGED"
          if echo "$CHANGED" | grep -qE '^(services|helm|infra|scripts)/'; then
            echo "relevant=true" >> "$GITHUB_OUTPUT"
            echo "Deployable surface changed -> integration test required."
          else
            echo "relevant=false" >> "$GITHUB_OUTPUT"
            echo "No deployable-surface changes -> integration test will skip."
          fi

  integration-test:
    needs: changes
    if: github.event_name == 'merge_group' && needs.changes.outputs.relevant == 'true'
    runs-on: ubuntu-latest
    container:
      image: ghcr.io/kakhavai/foundry/ci-runner:latest
      options: --privileged -v /var/run/docker.sock:/var/run/docker.sock
    permissions:
      contents: read

    steps:
      - uses: actions/checkout@v4

      - name: Create Kind cluster
        run: kind create cluster --config infra/kind/cluster.yaml --name foundry

      - name: Fix kubeconfig for container networking
        run: |
          # GHA container jobs run on a 'github_network_*' bridge; Kind nodes are on the
          # 'kind' network. Instead of connecting this container to Kind (requires knowing
          # our own container ID, unreliable on cgroup v2), connect the Kind control-plane
          # node to our network — no self-ID needed.
          GHA_NETWORK=$(docker network ls --format '{{.Name}}' | grep 'github_network' | head -1)
          docker network connect "$GHA_NETWORK" foundry-control-plane
          NODE_IP=$(docker inspect foundry-control-plane \
            --format="{{(index .NetworkSettings.Networks \"$GHA_NETWORK\").IPAddress}}")
          # --insecure-skip-tls-verify: Kind's cert SANs only include the Kind-network IP
          # (172.19.x.x), not the GHA-network IP we're now connecting through.
          kubectl config set-cluster kind-foundry \
            --server="https://$NODE_IP:6443" \
            --insecure-skip-tls-verify=true

      - name: Deploy observability stack
        run: |
          cd infra/grafana-stack
          helmfile repos
          helmfile apply

      - name: Deploy services
        run: |
          python3 scripts/deploy-local.py weather
          python3 scripts/deploy-local.py player-projections

      - name: Wait for pods ready
        run: |
          kubectl wait --for=condition=ready pod \
            --all -n monitoring --timeout=180s
          kubectl wait --for=condition=ready pod \
            -l app.kubernetes.io/name=weather --timeout=120s
          kubectl wait --for=condition=ready pod \
            -l app.kubernetes.io/name=player-projections --timeout=120s

      - name: Smoke test services
        run: bash scripts/smoke-test.sh

      - name: Tear down cluster
        if: always()
        run: kind delete cluster --name foundry
```

Changes from the old file: removed the `labeled` trigger type; added the `merge_group` trigger; added the `changes` job; gated `integration-test` on `merge_group` + relevance; removed the `if: contains(... 'ready-for-merge')` guard; removed the `pull-requests: write` permission; deleted the "Remove label on failure" step.

- [ ] **Step 2: Verify the YAML parses**

Run (Bash tool):
```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/integration-test.yml')); print('YAML OK')"
```
Expected: `YAML OK` (no traceback).

- [ ] **Step 3: Verify the relevance regex behaves — positive cases**

Run (Bash tool):
```bash
printf '%s\n' services/weather/main.py helm/values/weather/values.yaml infra/kind/cluster.yaml scripts/smoke-test.sh \
  | grep -qE '^(services|helm|infra|scripts)/' && echo "RELEVANT-PASS" || echo "RELEVANT-FAIL"
```
Expected: `RELEVANT-PASS`

- [ ] **Step 4: Verify the relevance regex behaves — negative cases**

Run (Bash tool):
```bash
printf '%s\n' docs/onboarding.md README.md .github/workflows/weather.yml \
  | grep -qE '^(services|helm|infra|scripts)/' && echo "IRRELEVANT-FAIL" || echo "IRRELEVANT-PASS"
```
Expected: `IRRELEVANT-PASS` (no deployable path matched).

- [ ] **Step 5: Verify all label machinery is gone**

Run (Bash tool):
```bash
grep -nE 'ready-for-merge|labeled|removeLabel|pull-requests: write' .github/workflows/integration-test.yml \
  && echo "LABEL-RESIDUE-FOUND" || echo "LABEL-FULLY-REMOVED"
```
Expected: `LABEL-FULLY-REMOVED`

- [ ] **Step 6: (Optional) actionlint**

If `actionlint` is installed (`actionlint --version` succeeds), run:
```bash
actionlint .github/workflows/integration-test.yml
```
Expected: no output (no errors). If actionlint is not installed, skip — the required `superpowers:pr-uat` run before the PR covers CI action validation.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/integration-test.yml
git commit -m "feat(ci): gate integration-test via merge queue instead of label

Runs the heavy Kind test only on merge_group for deployable-surface
changes; skips (= passes) on PRs and on docs/CI-only queue entries.
Removes the ready-for-merge label machinery."
```

---

## Task 2: Rewrite the CLAUDE.md "How CI Works" integration-test paragraph

**Files:**
- Modify: `CLAUDE.md` (the "How CI Works" section)

- [ ] **Step 1: Replace the label paragraph**

Find this exact paragraph in `CLAUDE.md`:

```
There is also a **required** `integration-test` check that gate-keeps merges. It spins up a Kind cluster, deploys the full stack, and runs `scripts/smoke-test.sh`. **It only runs when the `ready-for-merge` label is applied to the PR.** Without that label the check never fires and `gh pr merge` will fail with "Required status check 'integration-test' is expected." Always add the label after the other checks pass.
```

Replace it with:

```
There is also a **required** `integration-test` check that gate-keeps merges, enforced through a **GitHub merge queue**. It spins up a Kind cluster, deploys the full stack, and runs `scripts/smoke-test.sh`. On a PR the check reports a skipped pass (zero compute), so the PR is mergeable into the queue. The real test runs on the `merge_group` event when you mark a PR "Merge when ready": GitHub rebuilds the PR on top of the latest `main` and runs the integration test against that combined ref, merging only if it passes. If the change set does not touch the deployable surface (`services/`, `helm/`, `infra/`, `scripts/`), the test auto-skips in the queue and the PR merges immediately. There is no `ready-for-merge` label — merge intent is expressed by adding the PR to the queue.
```

- [ ] **Step 2: Check for other `ready-for-merge` references across the repo**

Run (Bash tool):
```bash
grep -rniE 'ready-for-merge|ready for merge' --include='*.md' . | grep -v 'docs/plans/2026-06-10-integration-test-merge-queue'
```
Expected: no remaining references that describe the label as the merge gate. If any are found (e.g. in `docs/onboarding.md`), update them to point at the merge-queue flow using the same wording as Step 1. If the command returns nothing, this step is complete.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(ci): describe merge-queue gate, drop label claim from CLAUDE.md"
```

---

## Task 3 (POST-MERGE, operational): Enable the merge_queue ruleset rule

> Do this ONLY after Tasks 1–2 are merged to `main`. Confirm with the user immediately before running the PUT — it is a production branch-protection change (reversible by removing the rule).

**Files:** none (GitHub-side ruleset `id=17252415`).

- [ ] **Step 1: Snapshot the current ruleset (rollback safety)**

Run (Bash tool):
```bash
gh api repos/kakhavai/foundry/rulesets/17252415 > /tmp/mainb-ruleset.before.json
cat /tmp/mainb-ruleset.before.json | python -m json.tool | head -5
```
Expected: prints the ruleset JSON head; backup saved.

- [ ] **Step 2: Build the updated ruleset payload (append merge_queue, preserve everything else)**

Run (Bash tool):
```bash
jq '{name, target, enforcement, conditions, bypass_actors,
     rules: (.rules + [{
       "type": "merge_queue",
       "parameters": {
         "merge_method": "SQUASH",
         "grouping_strategy": "ALLGREEN",
         "max_entries_to_build": 5,
         "max_entries_to_merge": 5,
         "min_entries_to_merge": 1,
         "min_entries_to_merge_wait_minutes": 5,
         "check_response_timeout_minutes": 60
       }
     }])}' /tmp/mainb-ruleset.before.json > /tmp/mainb-ruleset.after.json
jq '.rules[].type' /tmp/mainb-ruleset.after.json
```
Expected output lists: `"deletion"`, `"non_fast_forward"`, `"pull_request"`, `"required_status_checks"`, `"merge_queue"`.

Rationale for params: `merge_method: SQUASH` matches the existing `allowed_merge_methods: ["squash"]`; `ALLGREEN` requires every required check green before merge; `check_response_timeout_minutes: 60` comfortably exceeds the Kind test runtime; low entry counts suit a low-traffic repo.

- [ ] **Step 3: Apply the update**

Run (Bash tool):
```bash
gh api -X PUT repos/kakhavai/foundry/rulesets/17252415 --input /tmp/mainb-ruleset.after.json > /dev/null && echo "RULESET-UPDATED"
```
Expected: `RULESET-UPDATED`

- [ ] **Step 4: Verify the rule is active and the required check is preserved**

Run (Bash tool):
```bash
gh api repos/kakhavai/foundry/rulesets/17252415 --jq '.rules[] | select(.type=="merge_queue" or .type=="required_status_checks")'
```
Expected: shows the `merge_queue` rule with `merge_method: SQUASH` AND the `required_status_checks` rule still listing `integration-test`.

**Rollback if needed:** `gh api -X PUT repos/kakhavai/foundry/rulesets/17252415 --input /tmp/mainb-ruleset.before.json`

---

## Task 4 (POST-MERGE, operational): Rollout verification with throwaway PRs

> Merge-queue behavior cannot be exercised locally — it only exists on real PRs against `main`. Run these three checks after Task 3.

- [ ] **Step 1: Docs-only PR auto-merges without a Kind run**

Open a PR that changes only a docs file (e.g. add a line to `README.md`). Confirm:
- The `integration-test` check shows as **skipped/green** on the PR.
- The PR is queueable ("Merge when ready" is available).
- After queuing, the `merge_group` run shows `changes` → `relevant=false` and `integration-test` skipped, and the PR merges **without** spinning up Kind.

- [ ] **Step 2: Deployable-surface PR runs the real test before merging**

Open a PR touching `services/**` (e.g. a no-op comment in `services/weather/`). Confirm:
- On queuing, the `merge_group` run executes the full `integration-test` job (Kind cluster created, smoke test runs).
- The PR merges only after the test passes.

- [ ] **Step 3: A failing test ejects the PR from the queue**

On a `services/**` PR, temporarily introduce a change that fails the smoke test (or confirm via a known-failing branch). Confirm:
- The `merge_group` `integration-test` job fails.
- The PR is **removed from the queue and not merged**.
- Revert the failing change.

- [ ] **Step 4: Record results**

Note the outcomes in the PR description or a follow-up comment so the rollout is auditable. If Step 1 reveals that a skipped required check does NOT permit queue entry (contrary to GitHub's documented "skipped = success" semantics), fall back to the gate-job design from the spec's "label" alternative — but this is not expected.

---

## Pre-PR Gate

- [ ] **Run `superpowers:pr-uat` before opening the PR for Tasks 1–2.** This is mandatory per repo policy (CLAUDE.md PR Workflow). It validates the workflow YAML, CI action references, and that the docs render — the relevant subset for a CI-only change.

---

## Self-Review Notes

- **Spec coverage:** workflow rewrite (Task 1) ✔, path scope `services|helm|infra|scripts` (Task 1 regex) ✔, merge_group diff via base/head sha (Task 1) ✔, label removal (Task 1) ✔, CLAUDE.md rewrite (Task 2) ✔, merge_queue ruleset rule + squash + keep required check (Task 3) ✔, rollout verification 3 checks (Task 4) ✔, sequencing constraint (header + Task 3 gate) ✔.
- **No placeholders:** all steps carry exact file content, commands, and expected output.
- **Type/name consistency:** job names `changes`/`integration-test`, output `relevant`, regex `^(services|helm|infra|scripts)/`, and ruleset id `17252415` are used identically across all tasks.

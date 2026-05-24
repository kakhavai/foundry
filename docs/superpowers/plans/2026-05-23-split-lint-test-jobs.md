# Split Lint and Test CI Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the combined `python-lint-test` composite action with two focused actions (`python-lint`, `python-test`) and update both service workflows to run lint and test as parallel independent jobs.

**Architecture:** The current `python-lint-test` action runs ruff and pytest in sequence in a single CI job. Split into `python-lint` (ruff check + format check) and `python-test` (pytest), each with their own `uv sync` setup. Both workflow files (`github-stats.yml`, `platform-health.yml`) get a `lint` job and a `test` job that run in parallel; `build-push` gains both as dependencies alongside `helm-lint`. The old `python-lint-test` action is deleted since all callers are updated.

**Tech Stack:** GitHub Actions composite actions, uv, ruff, pytest, azure/setup-helm@v5

---

## File Map

**Create:**
- `.github/actions/python-lint/action.yml` — composite action: setup-uv, uv sync, ruff check, ruff format check
- `.github/actions/python-test/action.yml` — composite action: setup-uv, uv sync, pytest

**Modify:**
- `.github/workflows/github-stats.yml` — replace `lint-test` job with `lint` + `test` jobs; update `build-push` needs
- `.github/workflows/platform-health.yml` — same

**Delete:**
- `.github/actions/python-lint-test/action.yml` — no longer needed; all callers updated

---

## Task 1: Create `python-lint` and `python-test` composite actions; delete `python-lint-test`

**Files:**
- Create: `.github/actions/python-lint/action.yml`
- Create: `.github/actions/python-test/action.yml`
- Delete: `.github/actions/python-lint-test/action.yml`

- [ ] **Step 1: Create `.github/actions/python-lint/action.yml`**

```yaml
name: Python lint
description: Runs ruff lint and format check for a Python service using uv

inputs:
  working-directory:
    required: true
    description: Path to the Python service root relative to repo root (e.g. services/github-stats)

runs:
  using: composite
  steps:
    - uses: astral-sh/setup-uv@v5
    - name: Install dependencies
      shell: bash
      run: uv sync --frozen
      working-directory: ${{ inputs.working-directory }}
    - name: Lint
      shell: bash
      run: uv run ruff check .
      working-directory: ${{ inputs.working-directory }}
    - name: Format check
      shell: bash
      run: uv run ruff format --check .
      working-directory: ${{ inputs.working-directory }}
```

- [ ] **Step 2: Create `.github/actions/python-test/action.yml`**

```yaml
name: Python test
description: Runs pytest for a Python service using uv

inputs:
  working-directory:
    required: true
    description: Path to the Python service root relative to repo root (e.g. services/github-stats)

runs:
  using: composite
  steps:
    - uses: astral-sh/setup-uv@v5
    - name: Install dependencies
      shell: bash
      run: uv sync --frozen
      working-directory: ${{ inputs.working-directory }}
    - name: Test
      shell: bash
      run: uv run pytest
      working-directory: ${{ inputs.working-directory }}
```

- [ ] **Step 3: Delete `.github/actions/python-lint-test/action.yml`**

```bash
git rm .github/actions/python-lint-test/action.yml
```

- [ ] **Step 4: Validate both new YAML files**

```bash
python -c "
import yaml
yaml.safe_load(open('.github/actions/python-lint/action.yml'))
yaml.safe_load(open('.github/actions/python-test/action.yml'))
print('valid')
"
```

Expected: `valid`

- [ ] **Step 5: Commit**

```bash
git add .github/actions/python-lint/ .github/actions/python-test/
git commit -m "feat: split python-lint-test composite action into python-lint and python-test"
```

---

## Task 2: Update `github-stats.yml` — separate lint and test jobs

**Files:**
- Modify: `.github/workflows/github-stats.yml`

- [ ] **Step 1: Replace the full contents of `.github/workflows/github-stats.yml`**

```yaml
name: github-stats

on:
  pull_request:
    paths:
      - 'services/github-stats/**'
      - 'helm/values/github-stats/**'
      - 'helm/charts/generic-service/**'
      - '.github/actions/**'
  push:
    branches:
      - main
    paths:
      - 'services/github-stats/**'
      - 'helm/values/github-stats/**'
      - 'helm/charts/generic-service/**'
      - '.github/actions/**'

jobs:
  lint:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-lint
        with:
          working-directory: services/github-stats

  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-test
        with:
          working-directory: services/github-stats

  helm-lint:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/helm-lint
        with:
          chart-path: helm/charts/generic-service
          values-file: helm/values/github-stats/values.yaml

  build-push:
    runs-on: ubuntu-latest
    needs: [lint, test, helm-lint]
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/build-push
        with:
          service: github-stats
          image-name: ghcr.io/kakhavai/foundry/github-stats
          tag: ${{ github.sha }}
```

- [ ] **Step 2: Validate YAML**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/github-stats.yml')); print('valid')"
```

Expected: `valid`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/github-stats.yml
git commit -m "ci: split github-stats CI into separate lint, test, helm-lint jobs"
```

---

## Task 3: Update `platform-health.yml` — separate lint and test jobs

**Files:**
- Modify: `.github/workflows/platform-health.yml`

- [ ] **Step 1: Replace the full contents of `.github/workflows/platform-health.yml`**

```yaml
name: platform-health

on:
  pull_request:
    paths:
      - 'services/platform-health/**'
      - 'helm/values/platform-health/**'
      - 'helm/charts/generic-service/**'
      - '.github/actions/**'
  push:
    branches:
      - main
    paths:
      - 'services/platform-health/**'
      - 'helm/values/platform-health/**'
      - 'helm/charts/generic-service/**'
      - '.github/actions/**'

jobs:
  lint:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-lint
        with:
          working-directory: services/platform-health

  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-test
        with:
          working-directory: services/platform-health

  helm-lint:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/helm-lint
        with:
          chart-path: helm/charts/generic-service
          values-file: helm/values/platform-health/values.yaml

  build-push:
    runs-on: ubuntu-latest
    needs: [lint, test, helm-lint]
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/build-push
        with:
          service: platform-health
          image-name: ghcr.io/kakhavai/foundry/platform-health
          tag: ${{ github.sha }}
```

- [ ] **Step 2: Validate YAML**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/platform-health.yml')); print('valid')"
```

Expected: `valid`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/platform-health.yml
git commit -m "ci: split platform-health CI into separate lint, test, helm-lint jobs"
```

---

## Self-Review

**Spec coverage:**
- ✅ `python-lint` composite action — Task 1
- ✅ `python-test` composite action — Task 1
- ✅ `python-lint-test` deleted — Task 1
- ✅ `github-stats.yml` — separate `lint`, `test`, `helm-lint` jobs + `build-push` needs all three — Task 2
- ✅ `platform-health.yml` — same structure — Task 3

**Placeholder scan:** None found.

**Type consistency:** Both new actions use `working-directory` as the input name, consistent with the deleted `python-lint-test` action and the caller workflows.

**Note on `uv sync` duplication:** Both `python-lint` and `python-test` run `uv sync --frozen`. This is intentional — they run as separate jobs on separate runners, each needing a clean environment. Caching via uv's built-in GHA cache (handled automatically by `astral-sh/setup-uv@v5`) keeps this fast.

**Also update `docs/onboarding.md`:** The onboarding doc says "copy `platform-health.yml`" as the template — that remains accurate since it's still the reference file. No doc update needed.

# github-stats Service Scaffold + Lint-Test CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the minimal github-stats Python service scaffold and a passing lint-test CI job on `feat/github-stats-scaffold-lint-ci`.

**Architecture:** A FastAPI service with a single `/health` endpoint, managed by uv with `pyproject.toml` as the single source of truth for deps and tooling config. CI runs via a single `ci.yml` caller that uses `dorny/paths-filter` to detect changed paths and conditionally calls a reusable `templates/python/lint-test.yml` — the only triggered workflow file in the repo.

**Tech Stack:** Python 3.12, FastAPI, uv, ruff (lint + format), pytest, pytest-asyncio, httpx, GitHub Actions (dorny/paths-filter@v3, astral-sh/setup-uv@v5, actions/checkout@v4)

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `services/github-stats/.python-version` | Create | Pins Python 3.12 for uv |
| `services/github-stats/pyproject.toml` | Create | Deps, ruff config, pytest config — single source of truth |
| `services/github-stats/uv.lock` | Generate + commit | Reproducible lockfile — must be committed for `uv sync --frozen` in CI |
| `services/github-stats/src/github_stats/__init__.py` | Create | Package marker |
| `services/github-stats/src/github_stats/main.py` | Create | FastAPI app, /health endpoint only |
| `services/github-stats/tests/test_health.py` | Create | One test: GET /health returns 200 + `{"status": "ok"}` |
| `.github/workflows/templates/python/lint-test.yml` | Create | Reusable workflow: ruff check, ruff format --check, pytest |
| `.github/workflows/ci.yml` | Create | Single triggered workflow — path detection + conditional job dispatch |
| `services/github-stats/.gitkeep` | Delete | Replaced by real files |
| `.github/workflows/.gitkeep` | Delete | Replaced by ci.yml |

---

### Task 1: Project scaffold

**Files:**
- Create: `services/github-stats/.python-version`
- Create: `services/github-stats/pyproject.toml`
- Generate + commit: `services/github-stats/uv.lock`
- Delete: `services/github-stats/.gitkeep`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p services/github-stats/src/github_stats
mkdir -p services/github-stats/tests
```

- [ ] **Step 2: Pin Python version**

Create `services/github-stats/.python-version`:

```
3.12
```

- [ ] **Step 3: Create pyproject.toml**

Create `services/github-stats/pyproject.toml`:

```toml
[project]
name = "github-stats"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
]

[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "httpx",
]

[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
# E=pycodestyle errors, F=pyflakes, I=isort
# Full rule reference: https://docs.astral.sh/ruff/rules/
select = ["E", "F", "I"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 4: Install dependencies and generate lockfile**

```bash
cd services/github-stats
uv sync
```

Expected: uv resolves deps, creates `uv.lock`, no errors.

- [ ] **Step 5: Commit scaffold**

```bash
git add services/github-stats/.python-version services/github-stats/pyproject.toml services/github-stats/uv.lock
git rm services/github-stats/.gitkeep
git commit -m "chore: add github-stats project scaffold with uv"
```

---

### Task 2: Write failing health test

**Files:**
- Create: `services/github-stats/tests/test_health.py`

- [ ] **Step 1: Create test file**

Create `services/github-stats/tests/test_health.py`:

```python
from fastapi.testclient import TestClient
from github_stats.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test — verify it fails**

```bash
cd services/github-stats
uv run pytest tests/test_health.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'github_stats'`

- [ ] **Step 3: Commit failing test**

```bash
git add services/github-stats/tests/test_health.py
git commit -m "test: add failing health endpoint test"
```

---

### Task 3: Implement health endpoint

**Files:**
- Create: `services/github-stats/src/github_stats/__init__.py`
- Create: `services/github-stats/src/github_stats/main.py`

- [ ] **Step 1: Create package marker**

Create `services/github-stats/src/github_stats/__init__.py` — empty file.

- [ ] **Step 2: Implement health endpoint**

Create `services/github-stats/src/github_stats/main.py`:

```python
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 3: Run test — verify it passes**

```bash
cd services/github-stats
uv run pytest tests/test_health.py -v
```

Expected:

```
PASSED tests/test_health.py::test_health_returns_ok
1 passed in 0.XXs
```

- [ ] **Step 4: Run ruff lint**

```bash
cd services/github-stats
uv run ruff check .
```

Expected: no output (no violations).

- [ ] **Step 5: Run ruff format check**

```bash
cd services/github-stats
uv run ruff format --check .
```

Expected: `All checks passed!`

If violations are reported, fix formatting automatically and re-check:

```bash
uv run ruff format .
uv run ruff format --check .
```

- [ ] **Step 6: Commit implementation**

```bash
git add services/github-stats/src/github_stats/__init__.py services/github-stats/src/github_stats/main.py
git commit -m "feat: add github-stats service with /health endpoint"
```

---

### Task 4: Reusable lint-test CI template

**Files:**
- Create: `.github/workflows/templates/python/lint-test.yml`

- [ ] **Step 1: Create template directory**

```bash
mkdir -p .github/workflows/templates/python
```

- [ ] **Step 2: Create reusable workflow**

Create `.github/workflows/templates/python/lint-test.yml`:

```yaml
name: Python lint and test

on:
  workflow_call:
    inputs:
      working-directory:
        required: true
        type: string

jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
        working-directory: ${{ inputs.working-directory }}
      - run: uv run ruff check .
        working-directory: ${{ inputs.working-directory }}
      - run: uv run ruff format --check .
        working-directory: ${{ inputs.working-directory }}
      - run: uv run pytest
        working-directory: ${{ inputs.working-directory }}
```

- [ ] **Step 3: Commit template**

```bash
git add .github/workflows/templates/python/lint-test.yml
git commit -m "ci: add reusable Python lint-test workflow template"
```

---

### Task 5: CI caller workflow

**Files:**
- Create: `.github/workflows/ci.yml`
- Delete: `.github/workflows/.gitkeep`

- [ ] **Step 1: Create caller workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      github-stats: ${{ steps.filter.outputs.github-stats }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            github-stats:
              - 'services/github-stats/**'
              - 'helm/charts/github-stats/**'

  github-stats-lint-test:
    needs: changes
    if: needs.changes.outputs.github-stats == 'true'
    uses: ./.github/workflows/templates/python/lint-test.yml
    with:
      working-directory: services/github-stats
```

- [ ] **Step 2: Remove .gitkeep and commit**

```bash
git add .github/workflows/ci.yml
git rm .github/workflows/.gitkeep
git commit -m "ci: add CI caller workflow with dorny/paths-filter"
```

- [ ] **Step 3: Push and verify CI**

```bash
git push
```

Go to `https://github.com/kakhavai/foundry/actions` and verify:

- The `CI` workflow triggered on push
- The `changes` job completed
- The `github-stats-lint-test` job ran (files under `services/github-stats/**` changed)
- All steps green: `uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest`

---

## Success Criteria

- [ ] `uv sync` completes without errors from `services/github-stats/`
- [ ] `uv run pytest` passes: 1 test, 0 failures
- [ ] `uv run ruff check .` reports no violations
- [ ] `uv run ruff format --check .` reports `All checks passed!`
- [ ] Pushing to `feat/github-stats-scaffold-lint-ci` triggers the `CI` workflow
- [ ] `github-stats-lint-test` job passes in GitHub Actions
- [ ] `ci.yml` is the only triggered workflow file in `.github/workflows/`

# Design: github-stats Service Scaffold + Lint-Test CI

**Date:** 2026-04-22
**Branch:** one branch, one PR
**Phase:** 1 — First Paved Road

---

## Scope

Stand up the minimal `github-stats` Python service scaffold and a working `lint-test` CI job. This is the first piece of Phase 1 — subsequent PRs add `build-push` and `helm-lint` on top of this foundation.

Out of scope: GitHub API calls, OTel instrumentation, Docker image, Helm chart. Those come in later PRs.

---

## Decisions

### Monorepo
Foundry is a monorepo. All services, charts, and infra live here. Shared CI templates are colocated in `.github/workflows/templates/` and referenced via relative paths. No org-level `.github` repo needed.

### Dependency management
`uv` with `pyproject.toml` as the single source of truth. Prod deps under `[project.dependencies]`, dev deps under `[dependency-groups.dev]`. Auto-generated `uv.lock` handles reproducibility. No separate `requirements.txt` files.

### CI structure
One triggered workflow file (`ci.yml`) that never gets duplicated. Uses `dorny/paths-filter` to detect which services changed and conditionally calls reusable templates. Reusable workflows live in `templates/` organized by runtime/tool — not by service.

### Template organization
```
.github/workflows/templates/
├── python/       # Python-specific: lint, test
├── docker/       # Docker-specific: build, push  (future PR)
└── helm/         # Helm-specific: lint           (future PR)
```

`python/lint-test.yml` accepts a `working-directory` input so it works for any Python service in the monorepo.

---

## File Structure

```
services/github-stats/
├── pyproject.toml
├── uv.lock
├── src/
│   └── github_stats/
│       ├── __init__.py
│       └── main.py
└── tests/
    └── test_health.py

.github/workflows/
├── ci.yml
└── templates/
    └── python/
        └── lint-test.yml
```

---

## Service Scaffold

### `pyproject.toml`

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
select = ["E", "F", "I"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
```

### `src/github_stats/main.py`

FastAPI app with a single endpoint:

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok"}
```

### `tests/test_health.py`

```python
from fastapi.testclient import TestClient
from github_stats.main import app

client = TestClient(app)

def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

---

## CI Workflows

### `.github/workflows/ci.yml`

Single triggered workflow. Detects changed paths, calls reusable templates conditionally.

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

### `.github/workflows/templates/python/lint-test.yml`

Reusable. Parameterized by `working-directory` so any Python service can use it.

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
      - run: uv run pytest
        working-directory: ${{ inputs.working-directory }}
```

---

## Adding a New Service

When the next Python service lands:

1. Add a filter entry in `ci.yml` under `changes`
2. Add a conditional job block calling the same `templates/python/lint-test.yml`
3. Zero new workflow files

---

## Success Criteria

- `uv sync` works locally from `services/github-stats/`
- `uv run ruff check .` passes with no violations
- `uv run pytest` passes with one test
- Pushing a change to `services/github-stats/**` triggers `github-stats-lint-test` in CI
- Pushing a change outside `services/github-stats/**` skips the job
- `ci.yml` remains the only triggered workflow file

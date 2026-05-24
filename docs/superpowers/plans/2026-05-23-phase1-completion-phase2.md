# Phase 1 Completion + Phase 2 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Phase 1 (doc fixes, github-stats service implementation, build-push CI action) and lay Phase 2 foundation (second service scaffold + onboarding docs).

**Architecture:** github-stats gets GitHub API endpoints + OTel traces (OTLP gRPC → collector) + Prometheus metrics (OTel → Prometheus exporter → `/metrics`). The Grafana dashboard already expects OTel semantic convention metric names (`http_server_request_duration_seconds`), so metrics must come from the OTel FastAPI instrumentor via `PrometheusMetricReader`. A `build-push` composite action is added so CI produces immutable GHCR images tagged with Git SHA. Phase 2 adds a second service (`platform-health`) onboarded through the exact same pattern, plus service contract and onboarding docs.

**Tech Stack:** Python 3.12, FastAPI, httpx, opentelemetry-sdk, opentelemetry-instrumentation-fastapi, opentelemetry-instrumentation-httpx, opentelemetry-exporter-otlp-proto-grpc, opentelemetry-exporter-prometheus, prometheus-client, respx (test mocking), uv, GitHub Actions composite actions, docker/build-push-action@v6

---

## File Map

**Task 1 — Doc fixes:**
- Modify: `docs/architecture/architecture-overview.md`
- Modify: `docs/plans/2026-04-12-platform-design.md`

**Task 2 — GitHub API endpoints:**
- Create: `services/github-stats/src/github_stats/github.py`
- Modify: `services/github-stats/src/github_stats/main.py`
- Modify: `services/github-stats/pyproject.toml`
- Create: `services/github-stats/tests/conftest.py`
- Create: `services/github-stats/tests/test_activity.py`
- Create: `services/github-stats/tests/test_stats.py`

**Task 3 — OTel + Prometheus metrics:**
- Create: `services/github-stats/src/github_stats/telemetry.py`
- Modify: `services/github-stats/src/github_stats/main.py`
- Modify: `services/github-stats/pyproject.toml`
- Create: `services/github-stats/tests/test_metrics.py`

**Task 4 — build-push composite action:**
- Create: `.github/actions/build-push/action.yml`
- Modify: `.github/workflows/github-stats.yml`

**Task 5 — platform-health second service:**
- Create: `services/platform-health/pyproject.toml`
- Create: `services/platform-health/src/platform_health/__init__.py`
- Create: `services/platform-health/src/platform_health/main.py`
- Create: `services/platform-health/tests/test_health.py`
- Create: `services/platform-health/Dockerfile`
- Create: `helm/values/platform-health/values.yaml`
- Create: `.github/workflows/platform-health.yml`

**Task 6 — Phase 2 docs:**
- Create: `docs/service-contract.md`
- Create: `docs/onboarding.md`

---

## Task 1: Fix stale `_service-template.yml` doc references

The file `.github/workflows/_service-template.yml` was planned but never created. Per-service callers directly invoke composite actions. Two docs still describe the non-existent file.

**Files:**
- Modify: `docs/architecture/architecture-overview.md`
- Modify: `docs/plans/2026-04-12-platform-design.md`

- [ ] **Step 1: Fix `architecture-overview.md`**

Find this line in the Services section:

```
**CI:** A thin caller workflow (`.github/workflows/<service-name>.yml`) calls `.github/workflows/_service-template.yml`, delegating to composite actions for lint/test and Helm lint. Adding a service = add one caller file (~10 lines).
```

Replace with:

```
**CI:** A thin caller workflow (`.github/workflows/<service-name>.yml`) directly invokes composite actions for lint/test and Helm lint. Adding a service = add one caller file (~40 lines). The caller sets path filters and calls `.github/actions/python-lint-test` and `.github/actions/helm-lint`.
```

- [ ] **Step 2: Fix `platform-design.md`**

Find this row in the Decisions table:

```
| Shared platform infra | One base Helm chart (`generic-service`), one CI template (`_service-template.yml`), one observability stack | Avoids N-way duplication; adding a service requires two files, not a new chart directory |
```

Replace with:

```
| Shared platform infra | One base Helm chart (`generic-service`), shared composite actions (`.github/actions/`), one observability stack | Avoids N-way duplication; adding a service requires one CI caller file + one Helm values file |
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/architecture-overview.md docs/plans/2026-04-12-platform-design.md
git commit -m "docs: fix stale _service-template.yml references — actual CI uses composite actions directly"
```

---

## Task 2: GitHub API endpoints

Adds `GET /activity/{username}` and `GET /stats/{username}` to the github-stats service using httpx for async GitHub API calls.

**Files:**
- Create: `services/github-stats/src/github_stats/github.py`
- Modify: `services/github-stats/src/github_stats/main.py`
- Modify: `services/github-stats/pyproject.toml`
- Create: `services/github-stats/tests/conftest.py`
- Create: `services/github-stats/tests/test_activity.py`
- Create: `services/github-stats/tests/test_stats.py`

- [ ] **Step 1: Add httpx and respx to pyproject.toml**

In `services/github-stats/pyproject.toml`, update `dependencies` and `dev`:

```toml
[project]
name = "github-stats"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "httpx",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-prometheus",
    "opentelemetry-instrumentation-fastapi",
    "opentelemetry-instrumentation-httpx",
    "prometheus-client",
]

[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "httpx",
    "respx",
]
```

- [ ] **Step 2: Sync dependencies**

```bash
cd services/github-stats
uv sync
```

Expected: installs httpx and respx (other packages confirmed working)

- [ ] **Step 3: Write failing tests for `/activity/{username}`**

Create `services/github-stats/tests/test_activity.py`:

```python
import httpx
import respx
from fastapi.testclient import TestClient

from github_stats.main import app

client = TestClient(app)

MOCK_EVENTS = [
    {
        "type": "PushEvent",
        "repo": {"name": "testuser/repo-a"},
        "created_at": "2026-05-01T10:00:00Z",
        "payload": {"distinct_size": 3},
    },
    {
        "type": "PullRequestEvent",
        "repo": {"name": "testuser/repo-b"},
        "created_at": "2026-05-02T11:00:00Z",
        "payload": {},
    },
]


@respx.mock
def test_activity_returns_events_for_known_user():
    respx.get("https://api.github.com/users/testuser/events").mock(
        return_value=httpx.Response(200, json=MOCK_EVENTS)
    )
    response = client.get("/activity/testuser")
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert len(data["events"]) == 2
    assert data["events"][0]["type"] == "PushEvent"
    assert data["events"][0]["repo"] == "testuser/repo-a"
    assert data["events"][0]["created_at"] == "2026-05-01T10:00:00Z"


@respx.mock
def test_activity_returns_404_for_unknown_user():
    respx.get("https://api.github.com/users/nobody/events").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    response = client.get("/activity/nobody")
    assert response.status_code == 404


@respx.mock
def test_activity_caps_at_10_events():
    many_events = MOCK_EVENTS * 10  # 20 events
    respx.get("https://api.github.com/users/testuser/events").mock(
        return_value=httpx.Response(200, json=many_events)
    )
    response = client.get("/activity/testuser")
    assert response.status_code == 200
    assert len(response.json()["events"]) == 10
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
cd services/github-stats
uv run pytest tests/test_activity.py -v
```

Expected: FAIL — `ImportError` or 404 (routes don't exist yet)

- [ ] **Step 5: Write failing tests for `/stats/{username}`**

Create `services/github-stats/tests/test_stats.py`:

```python
import httpx
import respx
from fastapi.testclient import TestClient

from github_stats.main import app

client = TestClient(app)

MOCK_EVENTS = [
    {
        "type": "PushEvent",
        "repo": {"name": "testuser/repo-a"},
        "created_at": "2026-05-01T10:00:00Z",
        "payload": {"distinct_size": 3},
    },
    {
        "type": "PushEvent",
        "repo": {"name": "testuser/repo-a"},
        "created_at": "2026-05-02T09:00:00Z",
        "payload": {"distinct_size": 2},
    },
    {
        "type": "PullRequestEvent",
        "repo": {"name": "testuser/repo-b"},
        "created_at": "2026-05-03T14:00:00Z",
        "payload": {},
    },
]


@respx.mock
def test_stats_returns_summary_for_known_user():
    respx.get("https://api.github.com/users/testuser/events").mock(
        return_value=httpx.Response(200, json=MOCK_EVENTS)
    )
    response = client.get("/stats/testuser")
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert data["commit_count"] == 5  # 3 + 2
    assert data["pr_count"] == 1
    assert "testuser/repo-a" in data["top_repos"]


@respx.mock
def test_stats_returns_404_for_unknown_user():
    respx.get("https://api.github.com/users/nobody/events").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    response = client.get("/stats/nobody")
    assert response.status_code == 404
```

- [ ] **Step 6: Run stats tests to verify they fail**

```bash
uv run pytest tests/test_stats.py -v
```

Expected: FAIL — routes don't exist yet

- [ ] **Step 7: Create `github.py` — GitHub API client functions**

Create `services/github-stats/src/github_stats/github.py`:

```python
from collections import Counter

import httpx

GITHUB_API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def get_events(username: str, client: httpx.AsyncClient) -> list[dict]:
    response = await client.get(
        f"{GITHUB_API}/users/{username}/events", headers=HEADERS
    )
    response.raise_for_status()
    events = response.json()
    return [
        {
            "type": e["type"],
            "repo": e["repo"]["name"],
            "created_at": e["created_at"],
        }
        for e in events[:10]
    ]


async def get_stats_data(username: str, client: httpx.AsyncClient) -> dict:
    response = await client.get(
        f"{GITHUB_API}/users/{username}/events", headers=HEADERS
    )
    response.raise_for_status()
    events = response.json()

    push_events = [e for e in events if e["type"] == "PushEvent"]
    pr_events = [e for e in events if e["type"] == "PullRequestEvent"]
    repo_counts = Counter(e["repo"]["name"] for e in events)

    return {
        "username": username,
        "commit_count": sum(
            e["payload"].get("distinct_size", 0) for e in push_events
        ),
        "pr_count": len(pr_events),
        "top_repos": [repo for repo, _ in repo_counts.most_common(5)],
    }
```

- [ ] **Step 8: Add the new routes to `main.py`**

Replace the full contents of `services/github-stats/src/github_stats/main.py`:

```python
import httpx
from fastapi import FastAPI, HTTPException

from .github import get_events, get_stats_data

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/activity/{username}")
async def activity(username: str):
    async with httpx.AsyncClient() as client:
        try:
            events = await get_events(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return {"username": username, "events": events}


@app.get("/stats/{username}")
async def stats(username: str):
    async with httpx.AsyncClient() as client:
        try:
            data = await get_stats_data(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return data
```

- [ ] **Step 9: Run all tests and verify they pass**

```bash
uv run pytest -v
```

Expected: all tests pass including the original `test_health_returns_ok`

- [ ] **Step 10: Run lint**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: no violations

- [ ] **Step 11: Commit**

```bash
git add services/github-stats/
git commit -m "feat: add /activity and /stats endpoints to github-stats"
```

---

## Task 3: OTel traces + Prometheus metrics

Sets up OTel FastAPI auto-instrumentation (traces to OTLP, metrics via Prometheus exporter). The Grafana dashboard queries `http_server_request_duration_seconds` — this metric is generated automatically by the OTel FastAPI instrumentor when `PrometheusMetricReader` is configured. OTel is only initialized when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so tests run without it.

**Files:**
- Create: `services/github-stats/src/github_stats/telemetry.py`
- Modify: `services/github-stats/src/github_stats/main.py`
- Create: `services/github-stats/tests/test_metrics.py`

- [ ] **Step 1: Write failing test for `/metrics` endpoint**

Create `services/github-stats/tests/test_metrics.py`:

```python
from fastapi.testclient import TestClient

from github_stats.main import app

client = TestClient(app)


def test_metrics_endpoint_returns_prometheus_format():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_metrics.py -v
```

Expected: FAIL — 404 (route doesn't exist)

- [ ] **Step 3: Create `telemetry.py`**

Create `services/github-stats/src/github_stats/telemetry.py`:

```python
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_telemetry(app) -> None:
    resource = Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", "github-stats")}
    )

    # Traces → OTLP gRPC → OTel Collector → Tempo
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    trace.set_tracer_provider(tracer_provider)

    # Metrics → Prometheus format → /metrics endpoint
    # PrometheusMetricReader registers with prometheus_client's default registry.
    # The OTel FastAPI instrumentor generates http.server.request.duration,
    # which becomes http_server_request_duration_seconds in Prometheus format —
    # matching what the Grafana dashboard queries.
    reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
```

- [ ] **Step 4: Update `main.py` to add lifespan + `/metrics` endpoint**

Replace the full contents of `services/github-stats/src/github_stats/main.py`:

```python
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .github import get_events, get_stats_data


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Only set up OTel when running in Kubernetes (env var injected by ConfigMap).
    # Tests run without it so there are no collector connection errors.
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from .telemetry import setup_telemetry
        setup_telemetry(app)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/activity/{username}")
async def activity(username: str):
    async with httpx.AsyncClient() as client:
        try:
            events = await get_events(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return {"username": username, "events": events}


@app.get("/stats/{username}")
async def stats(username: str):
    async with httpx.AsyncClient() as client:
        try:
            data = await get_stats_data(username, client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="User not found")
            raise HTTPException(status_code=502, detail="GitHub API error")
    return data
```

- [ ] **Step 5: Run all tests**

```bash
uv run pytest -v
```

Expected: all tests pass

- [ ] **Step 6: Run lint**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: no violations

- [ ] **Step 7: Commit**

```bash
git add services/github-stats/
git commit -m "feat: add OTel traces + Prometheus metrics to github-stats"
```

---

## Task 4: build-push composite action

Creates `.github/actions/build-push/action.yml` and adds a `build-push` job to `github-stats.yml` that runs on pushes to `main` after lint and helm-lint pass.

**Files:**
- Create: `.github/actions/build-push/action.yml`
- Modify: `.github/workflows/github-stats.yml`

- [ ] **Step 1: Create `.github/actions/build-push/action.yml`**

```yaml
name: Build and push image
description: Builds a Docker image from a service directory and pushes it to GHCR

inputs:
  service:
    required: true
    description: Service name, used as the build context path (e.g. github-stats)
  image-name:
    required: true
    description: Full image name including registry (e.g. ghcr.io/owner/repo/service)
  tag:
    required: true
    description: Image tag (e.g. the Git SHA)

runs:
  using: composite
  steps:
    - uses: docker/setup-buildx-action@v3
    - uses: docker/login-action@v3
      with:
        registry: ghcr.io
        username: ${{ github.actor }}
        password: ${{ github.token }}
    - uses: docker/build-push-action@v6
      with:
        context: services/${{ inputs.service }}
        push: true
        tags: ${{ inputs.image-name }}:${{ inputs.tag }}
        cache-from: type=gha
        cache-to: type=gha,mode=max
```

- [ ] **Step 2: Update `.github/workflows/github-stats.yml` to add build-push job**

Replace the full contents of `.github/workflows/github-stats.yml`:

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
  lint-test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-lint-test
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
    needs: [lint-test, helm-lint]
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

- [ ] **Step 3: Validate YAML**

```bash
python -c "
import yaml
yaml.safe_load(open('.github/actions/build-push/action.yml'))
yaml.safe_load(open('.github/workflows/github-stats.yml'))
print('valid')
"
```

Expected: `valid`

- [ ] **Step 4: Commit**

```bash
git add .github/actions/build-push/ .github/workflows/github-stats.yml
git commit -m "feat: add build-push composite action and wire into github-stats CI"
```

---

## Task 5: platform-health second service (Phase 2)

A minimal FastAPI service that proves the platform pattern works for more than one service. Intentionally simple — the value is demonstrating zero-duplication onboarding via the existing CI and Helm infrastructure.

**Files:**
- Create: `services/platform-health/pyproject.toml`
- Create: `services/platform-health/src/platform_health/__init__.py`
- Create: `services/platform-health/src/platform_health/main.py`
- Create: `services/platform-health/tests/test_health.py`
- Create: `services/platform-health/Dockerfile`
- Create: `helm/values/platform-health/values.yaml`
- Create: `.github/workflows/platform-health.yml`

- [ ] **Step 1: Write failing tests for platform-health**

Create `services/platform-health/tests/test_health.py`:

```python
from fastapi.testclient import TestClient

from platform_health.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_service_name():
    response = client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["service"] == "platform-health"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd services/platform-health
# Tests will fail because the module doesn't exist yet
uv run pytest -v 2>&1 | head -20
```

Expected: error — module not found

- [ ] **Step 3: Create `pyproject.toml`**

Create `services/platform-health/pyproject.toml`:

```toml
[project]
name = "platform-health"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-prometheus",
    "opentelemetry-instrumentation-fastapi",
    "prometheus-client",
]

[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "httpx",
]

[tool.uv]
package = true

[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]

[project.scripts]
dev = "platform_health.cli:dev"
test = "platform_health.cli:test"
lint = "platform_health.cli:lint"
format = "platform_health.cli:fmt"
```

- [ ] **Step 4: Create the service package**

Create `services/platform-health/src/platform_health/__init__.py` (empty):

```python
```

- [ ] **Step 5: Create `main.py`**

Create `services/platform-health/src/platform_health/main.py`:

```python
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "platform-health")}
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                )
            )
        )
        trace.set_tracer_provider(tracer_provider)
        reader = PrometheusMetricReader()
        metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )
        FastAPIInstrumentor.instrument_app(app)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": "platform-health"}


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

- [ ] **Step 6: Create a `cli.py` for uv scripts**

Create `services/platform-health/src/platform_health/cli.py`:

```python
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent


def dev():
    subprocess.run(
        ["uvicorn", "platform_health.main:app", "--reload", "--host", "0.0.0.0", "--port", "8001"],
        cwd=ROOT,
    )


def test():
    subprocess.run(["pytest"] + sys.argv[1:], cwd=ROOT)


def lint():
    subprocess.run(["ruff", "check", "."], cwd=ROOT)


def fmt():
    subprocess.run(["ruff", "format", "."], cwd=ROOT)
```

- [ ] **Step 7: Sync and run tests**

```bash
cd services/platform-health
uv sync
uv run pytest -v
```

Expected: both tests pass

- [ ] **Step 8: Run lint**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: no violations

- [ ] **Step 9: Create `Dockerfile`**

Create `services/platform-health/Dockerfile`:

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install uv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
WORKDIR /app
RUN useradd --system --no-create-home appuser
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
ENV PATH="/app/.venv/bin:$PATH"
USER appuser
EXPOSE 8001
CMD ["uvicorn", "platform_health.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

- [ ] **Step 10: Create Helm values file**

Create `helm/values/platform-health/values.yaml`:

```yaml
service:
  name: platform-health
  port: 8001

image:
  repository: ghcr.io/kakhavai/foundry/platform-health

containerPort: 8001

resources:
  limits:
    cpu: 250m
    memory: 256Mi
  requests:
    cpu: 100m
    memory: 128Mi

otel:
  resourceAttributes: "service.name=platform-health,service.namespace=foundry"
```

- [ ] **Step 11: Verify Helm lint passes with platform-health values**

```bash
helm lint helm/charts/generic-service -f helm/values/platform-health/values.yaml
```

Expected: `1 chart(s) linted, 0 chart(s) failed`

- [ ] **Step 12: Create CI caller for platform-health**

Create `.github/workflows/platform-health.yml`:

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
  lint-test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-lint-test
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
    needs: [lint-test, helm-lint]
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

- [ ] **Step 13: Commit**

```bash
git add services/platform-health/ helm/values/platform-health/ .github/workflows/platform-health.yml
git commit -m "feat: add platform-health service — proves CI + Helm pattern reuse (Phase 2)"
```

---

## Task 6: Phase 2 docs

Documents the service contract (what every Foundry service must provide) and the onboarding guide (step-by-step instructions for adding a new service).

**Files:**
- Create: `docs/service-contract.md`
- Create: `docs/onboarding.md`

- [ ] **Step 1: Create `docs/service-contract.md`**

```markdown
# Service Contract

Every service deployed on Foundry must satisfy this contract. The platform provides the infrastructure; services provide the application.

---

## Required Endpoints

| Endpoint | Behavior |
|---|---|
| `GET /health` | Returns `{"status": "ok"}` with HTTP 200 when the service is running. Used by Kubernetes liveness and readiness probes. |
| `GET /metrics` | Returns Prometheus-format metrics. Scraped by Prometheus via pod annotations. |

---

## Required Dockerfile

- Multi-stage build. Final stage based on `python:3.12-slim`.
- Runs as a non-root user.
- Listens on the port declared in `helm/values/<service>/values.yaml` (`containerPort`).

---

## Required Helm Values File

Every service has exactly one file at `helm/values/<service-name>/values.yaml`. Minimum required fields:

```yaml
service:
  name: <service-name>   # must match the service directory name
  port: <port>

image:
  repository: ghcr.io/kakhavai/foundry/<service-name>

containerPort: <port>
```

The base chart (`helm/charts/generic-service`) injects OTel env vars and Prometheus pod annotations automatically. No observability config belongs in the values file.

---

## Required Kubernetes Labels

The base chart applies these labels to all resources automatically. Services do not configure them directly:

| Label | Value |
|---|---|
| `app.kubernetes.io/name` | value of `service.name` |
| `app.kubernetes.io/instance` | Helm release name |
| `app.kubernetes.io/managed-by` | `Helm` |

---

## OTel Instrumentation

Services receive OTel configuration as environment variables from the ConfigMap generated by the base chart:

| Variable | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | gRPC endpoint for the OTel Collector |
| `OTEL_SERVICE_NAME` | Set to `service.name` from Helm values |
| `OTEL_RESOURCE_ATTRIBUTES` | Additional resource attributes |

Services are responsible for initializing the OTel SDK and instrumenting their framework. See `services/github-stats/src/github_stats/telemetry.py` for the reference implementation.

Guard OTel initialization on `OTEL_EXPORTER_OTLP_ENDPOINT` so the service starts cleanly in local dev without a collector.
```

- [ ] **Step 2: Create `docs/onboarding.md`**

```markdown
# Onboarding a New Service

This guide walks through adding a new Python HTTP service to Foundry. The result: CI runs on every push, images are built and pushed to GHCR on merge to `main`, Helm deploys the service to the Kind cluster, and Grafana shows logs, traces, and metrics — with no observability config in the service.

---

## Prerequisites

- Docker running locally
- Kind cluster running (`kind create cluster --config infra/kind/cluster.yaml`)
- Observability stack deployed (`cd infra/grafana-stack && helmfile apply`)

---

## Step 1: Create the service directory

```bash
mkdir -p services/<service-name>/src/<service_name>
mkdir services/<service-name>/tests
```

Your service needs these files (see `services/platform-health/` as the reference):

```
services/<service-name>/
├── pyproject.toml
├── Dockerfile
├── src/<service_name>/
│   ├── __init__.py
│   ├── main.py        # FastAPI app
│   └── cli.py         # uv script entry points
└── tests/
    └── test_health.py
```

The service must satisfy the [service contract](service-contract.md):
- `GET /health` → `{"status": "ok"}`
- `GET /metrics` → Prometheus-format metrics
- OTel initialization guarded on `OTEL_EXPORTER_OTLP_ENDPOINT`

---

## Step 2: Add Helm values

Create `helm/values/<service-name>/values.yaml` with the service-specific values. Copy from `helm/values/platform-health/values.yaml` and change the name and port:

```yaml
service:
  name: <service-name>
  port: <port>           # pick a port not used by another service

image:
  repository: ghcr.io/kakhavai/foundry/<service-name>

containerPort: <port>

resources:
  limits:
    cpu: 250m
    memory: 256Mi
  requests:
    cpu: 100m
    memory: 128Mi

otel:
  resourceAttributes: "service.name=<service-name>,service.namespace=foundry"
```

Verify the chart lints cleanly:

```bash
helm lint helm/charts/generic-service -f helm/values/<service-name>/values.yaml
```

---

## Step 3: Add CI workflow

Copy `.github/workflows/platform-health.yml` to `.github/workflows/<service-name>.yml` and replace all occurrences of `platform-health` with your service name.

No CI logic lives in this file — it calls the shared composite actions.

---

## Step 4: Deploy locally

```bash
python scripts/deploy-local.py <service-name>
```

Or bring up the full stack including your service:

```bash
python scripts/stack-up.py <service-name>
```

---

## Step 5: Verify

```bash
# Service health
curl http://localhost:<port>/health

# Prometheus metrics
curl http://localhost:<port>/metrics

# Grafana — http://localhost:3000 (admin / admin)
# Traces and logs appear once the service handles requests
```

---

## What you get automatically

- **CI:** lint, test, Helm lint on every push; image pushed to GHCR on merge to `main`
- **Kubernetes:** Deployment + ClusterIP Service + ConfigMap with OTel env vars
- **Prometheus:** auto-discovered via pod annotations (`prometheus.io/scrape: "true"`)
- **OTel traces:** exported to the cluster-level Collector → Tempo
- **Logs:** collected by Loki from stdout
```

- [ ] **Step 3: Commit**

```bash
git add docs/service-contract.md docs/onboarding.md
git commit -m "docs: add service contract and onboarding guide (Phase 2)"
```

---

## Self-Review

**Spec coverage:**
- ✅ Fix `_service-template.yml` doc references (Task 1)
- ✅ `GET /activity/{username}` with GitHub API (Task 2)
- ✅ `GET /stats/{username}` with GitHub API (Task 2)
- ✅ OTel traces (OTLP gRPC → collector) (Task 3)
- ✅ `GET /metrics` Prometheus endpoint matching dashboard metric names (Task 3)
- ✅ `build-push` composite action + wired into github-stats CI (Task 4)
- ✅ Second service (`platform-health`) with same CI + Helm pattern (Task 5)
- ✅ `docs/service-contract.md` (Task 6)
- ✅ `docs/onboarding.md` (Task 6)
- ✅ `infra/grafana-stack/dashboards/service-template.json` — not included; Phase 2 doc listed it but the dashboard JSON is highly specific to service names and labels. A separate plan item is needed to create a parameterized template.

**Placeholder scan:** No TBD/TODO placeholders found. All code blocks are complete.

**Type consistency:**
- `get_events` and `get_stats_data` in `github.py` both take `(username: str, client: httpx.AsyncClient)` — consistent across Task 2 and 3
- `setup_telemetry(app)` signature is consistent between `telemetry.py` definition and `main.py` call
- `generate_latest()` from `prometheus_client` used consistently in both services

**Known gap:** `services/github-stats/src/github_stats/cli.py` exists per the `pyproject.toml` `[project.scripts]` section but was not shown in the file listing — it should already exist from earlier work. If it doesn't, copy the pattern from `platform-health/src/platform_health/cli.py`.

**Out of scope for this plan:** parameterized Grafana dashboard template (`service-template.json`), Argo CD / GitOps flow (Phase 3).

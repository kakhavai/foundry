# Phase 5A — Rigorous Service & Platform Testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing services provably correct under adversarial input and enforce that property in CI, so that Phase 5B (chaos) and 5C (adversarial agents) test the *platform* rather than rediscovering *service* bugs.

**Architecture:** Every new test suite runs inside the existing per-service `test` job — no new workflows, composite actions, or permissions. Contracts are enforced provider-side: JSON Schema for the `player-data` snapshot documents, committed OpenAPI snapshots for the HTTP services. Coverage is measured and reported from the first task but the `--cov-fail-under` gate is only switched on in Task 13, after the coverage-raising tasks land, so `main` stays green at every commit.

**Tech Stack:** pytest, pytest-asyncio (`asyncio_mode = "auto"`), pytest-cov, coverage.py 7.15+, Hypothesis, jsonschema, respx, FastAPI `TestClient`, uv, GitHub Actions composite actions, Helm 3.

**Reference design:** `docs/superpowers/specs/2026-07-27-phase-5a-rigorous-testing-design.md` (local, gitignored)
**Reference spec:** `docs/architecture/phase-5-resilience-and-ai-testing.md` (Stage 1)

---

## Global Constraints

- **Python** `>=3.12` for all services. Line length 88, ruff lint `select = ["E", "F", "I"]`.
- **Coverage gate is 80%** line + branch, with **no omitted files**. Applies to `weather`, `player-projections`, `foundry-cli`.
- **No new GitHub Actions workflows, composite actions, or workflow permissions.** The only workflow file created is `foundry-cli.yml` (Task 2), which exists because the package is currently untested in CI at all.
- **No new runtime dependencies.** Everything added lands in `[dependency-groups] dev`.
- **Do not use the term "NFL" anywhere.** The project does not hold those rights. Use "pro football" or "stadium".
- **Player IDs in contracts are opaque**, e.g. `p_8f3a21` — no league namespace.
- **Nothing in this phase may require `player-data` to exist.** Schemas encode the intended shape; they are validated against fixtures, not a live provider.
- **Every task ends with a commit.** Run `uv run ruff check .` and `uv run ruff format --check .` before each commit.
- **`superpowers:pr-uat` must be run before the final PR is opened** (required by `CLAUDE.md`).

---

## Measured Baseline

Recorded 2026-07-27 with `pytest --cov --cov-branch`. These are the numbers the plan closes against.

| Package | Total | File gaps |
|---|---|---|
| `weather` | **61%** | `client.py` 68%, `main.py` 80%, `telemetry.py` 0% |
| `player-projections` | **42%** | `main.py` 51%, `telemetry.py` 0%, `client.py` 100% |
| `foundry-cli` | **83%** | `cli.py` 0%; triage modules 86–100% |

Two defects were found while measuring. Both are fixed by this plan:

1. **`services/foundry-cli` does not build from a clean checkout.** `uv run pytest` fails with `Multiple top-level packages discovered in a flat-layout: ['eval', 'foundry']`. Phase 4 added the `eval/` directory alongside `foundry/`, and setuptools auto-discovery cannot disambiguate them. Fixed in Task 2.
2. **`services/foundry-cli` has no CI workflow.** No workflow file references it, so the Phase 4 triage engine — the code Phase 5C's pass/fail criteria depend on — has never run in CI. Fixed in Task 2.

---

## Design Correction Carried Into This Plan

The design proposed a service-side test asserting the OTel collector endpoint constant matches the Helm chart. **The services have no such constant** — `telemetry.py` reads `OTEL_EXPORTER_OTLP_ENDPOINT` from the environment and the value exists only in `helm/charts/generic-service/values.yaml`. The cross-file check is therefore a **Helm render assertion** (Task 12, in the repo-root `tests/`), not a Python service test. The service-side telemetry tests (Tasks 3 and 7) cover the env-var guard, resource attributes, and instrumentation attachment instead.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `.gitignore` | Modify | Ignore `.coverage`, `htmlcov/`, `.hypothesis/` |
| `.github/actions/python-test/action.yml` | Modify | Coverage measurement + job-summary report; gate flipped on in Task 13 |
| `.github/workflows/foundry-cli.yml` | Create | Lint + test for the triage engine (currently absent) |
| `services/weather/pyproject.toml` | Modify | Coverage config, dev deps |
| `services/weather/tests/test_telemetry.py` | Create | OTel guard, resource attrs, instrumentation attachment |
| `services/weather/tests/test_properties.py` | Create | Hypothesis suite for the Open-Meteo response parser |
| `services/weather/tests/test_contract.py` | Create | OpenAPI snapshot divergence detection |
| `services/weather/tests/integration/test_app.py` | Create | Real-HTTP suite: concurrency, timeout, malformed upstream |
| `services/player-projections/pyproject.toml` | Modify | Coverage config, dev deps |
| `services/player-projections/tests/test_telemetry.py` | Create | OTel guard, resource attrs, instrumentation attachment |
| `services/player-projections/tests/test_poll_loop.py` | Create | Background poll loop behaviour and failure handling |
| `services/player-projections/tests/test_contract.py` | Create | Snapshot schema validation + OpenAPI snapshot |
| `services/player-projections/tests/test_properties.py` | Create | Hypothesis suite for the snapshot parser |
| `services/player-projections/tests/integration/test_app.py` | Create | Real-HTTP suite |
| `services/player-projections/player_projections/client.py` | Modify | Raise a typed error on malformed payloads |
| `services/player-projections/player_projections/main.py` | Modify | Skip malformed player records instead of dropping the whole batch |
| `services/foundry-cli/pyproject.toml` | Modify | Packaging fix, coverage config, dev deps |
| `services/foundry-cli/tests/test_cli_entrypoint.py` | Create | Cover `cli.py` (currently 0%) |
| `contracts/player-data/{standard,half-ppr,ppr}.v1.schema.json` | Create | Snapshot contracts, one per scoring format |
| `contracts/player-data/fixtures/*.json` | Create | Valid and invalid sample payloads |
| `contracts/openapi/{weather,player-projections}.json` | Create | Committed OpenAPI snapshots |
| `tests/test_helm_otel_endpoint.py` | Create | Helm render assertion for the collector DNS name |
| `docs/adr/0002-provider-driven-contracts.md` | Create | Why schema-first over Pact; revisit trigger |
| `docs/testing-strategy.md` | Create | What is tested at each layer and why |
| `docs/architecture/phase-5-resilience-and-ai-testing.md` | Modify | Reconcile Stage 1 deliverables with what was built |
| `CLAUDE.md` | Modify | Resolve the `player-data` auth contradiction |
| `services/weather/README.md` | Modify | Remove trademark references |

---

## Task 1: Coverage tooling and reporting

Establishes measurement and reporting everywhere. **The gate is deliberately not enabled yet** — enabling it here would fail CI until Tasks 3–11 land.

**Files:**
- Modify: `.gitignore`
- Modify: `services/weather/pyproject.toml`
- Modify: `services/player-projections/pyproject.toml`
- Modify: `.github/actions/python-test/action.yml`

**Interfaces:**
- Consumes: nothing
- Produces: `uv run pytest` emits branch coverage for every service; CI writes a markdown coverage table to the job summary. Task 13 flips `--cov-fail-under=80` on.

- [ ] **Step 1: Ignore coverage and hypothesis artifacts**

Append to `.gitignore`:

```gitignore

# Test artifacts
.coverage
.coverage.*
htmlcov/
.hypothesis/
```

- [ ] **Step 2: Add coverage config to `services/weather/pyproject.toml`**

Add `pytest-cov` to the dev group and append the coverage sections. Replace the existing `[dependency-groups]` and `[tool.pytest.ini_options]` blocks:

```toml
[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "httpx",
    "respx",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "--cov=weather --cov-branch --cov-report=term"

[tool.coverage.run]
branch = true
source = ["weather"]

[tool.coverage.report]
show_missing = true
```

- [ ] **Step 3: Add the same coverage config to `services/player-projections/pyproject.toml`**

Identical, with `weather` replaced by `player_projections` (note the underscore — it is the Python package name):

```toml
[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "httpx",
    "respx",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "--cov=player_projections --cov-branch --cov-report=term"

[tool.coverage.run]
branch = true
source = ["player_projections"]

[tool.coverage.report]
show_missing = true
```

- [ ] **Step 4: Verify coverage now reports for both services**

```bash
cd services/weather && uv run pytest -q
cd ../player-projections && uv run pytest -q
```

Expected: both pass, each printing a coverage table. `weather` totals 61%, `player-projections` totals 42%. Neither fails — there is no gate yet.

- [ ] **Step 5: Add the job-summary report to the composite action**

Replace the `Test` step in `.github/actions/python-test/action.yml` with:

```yaml
    - name: Test
      shell: bash
      run: uv run pytest
      working-directory: ${{ inputs.working-directory }}
    - name: Coverage summary
      if: always()
      shell: bash
      run: |
        echo "### Coverage — ${{ inputs.working-directory }}" >> "$GITHUB_STEP_SUMMARY"
        uv run coverage report --format=markdown >> "$GITHUB_STEP_SUMMARY"
      working-directory: ${{ inputs.working-directory }}
```

`if: always()` makes the report appear even when the gate fails in Task 13 — that is exactly when you want to see it. `--format=markdown` is a `coverage report` option (coverage.py 6.5+, verified against 7.15.2); it is **not** a valid `pytest --cov-report` value.

- [ ] **Step 6: Commit**

```bash
git add .gitignore .github/actions/python-test/action.yml services/weather/pyproject.toml services/player-projections/pyproject.toml
git commit -m "test: measure and report branch coverage in CI"
```

---

## Task 2: Fix foundry-cli packaging and wire it into CI

The triage engine cannot be tested today — the package does not build, and no workflow runs it. This must land before any coverage gate can include it.

**Files:**
- Modify: `services/foundry-cli/pyproject.toml`
- Create: `.github/workflows/foundry-cli.yml`

**Interfaces:**
- Consumes: `.github/actions/python-test` (Task 1)
- Produces: `cd services/foundry-cli && uv run pytest` succeeds; a `foundry-cli` CI check exists.

- [ ] **Step 1: Reproduce the build failure**

```bash
cd services/foundry-cli && uv run pytest -q
```

Expected: FAIL with `Multiple top-level packages discovered in a flat-layout: ['eval', 'foundry']`.

- [ ] **Step 2: Constrain package discovery to the `foundry` package**

Append to `services/foundry-cli/pyproject.toml`:

```toml
[tool.setuptools.packages.find]
include = ["foundry*"]
```

`eval/` is a developer harness, not part of the distributed package, so excluding it is correct as well as necessary. `include = ["foundry*"]` keeps the `foundry.triage.*` subpackages.

- [ ] **Step 3: Verify the build and tests now succeed**

```bash
cd services/foundry-cli && uv run pytest -q
```

Expected: PASS, 30 tests.

- [ ] **Step 4: Add coverage config**

Replace the `[dependency-groups]` and `[tool.pytest.ini_options]` blocks in `services/foundry-cli/pyproject.toml`:

```toml
[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-cov",
    "respx",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--cov=foundry --cov-branch --cov-report=term"

[tool.coverage.run]
branch = true
source = ["foundry"]

[tool.coverage.report]
show_missing = true
```

- [ ] **Step 5: Confirm the coverage baseline**

```bash
cd services/foundry-cli && uv run pytest -q
```

Expected: PASS with TOTAL **83%**. `foundry/cli.py` reports 0% — closed in Task 11.

- [ ] **Step 6: Create the CI workflow**

Create `.github/workflows/foundry-cli.yml`. This mirrors `weather.yml` minus the build/push and GitOps jobs — `foundry-cli` is a developer tool, not a deployed service, so it has no image and no Helm chart.

```yaml
name: foundry-cli

on:
  pull_request:
    paths:
      - "services/foundry-cli/**"
      - ".github/workflows/foundry-cli.yml"
      - ".github/actions/python-lint/**"
      - ".github/actions/python-test/**"
  push:
    branches: [main]
    paths:
      - "services/foundry-cli/**"
      - ".github/workflows/foundry-cli.yml"

concurrency:
  group: foundry-cli-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-lint
        with:
          working-directory: services/foundry-cli

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-test
        with:
          working-directory: services/foundry-cli
```

- [ ] **Step 7: Verify the workflow parses and the action reference resolves**

```bash
python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/foundry-cli.yml')); print('ok')"
ls .github/actions/python-lint/action.yml .github/actions/python-test/action.yml
```

Expected: `ok`, and both action files exist.

- [ ] **Step 8: Commit**

```bash
git add services/foundry-cli/pyproject.toml .github/workflows/foundry-cli.yml
git commit -m "fix(foundry-cli): constrain package discovery, add missing CI workflow"
```

---

## Task 3: weather telemetry tests

Takes `weather/telemetry.py` off 0% with tests that assert wiring, not SDK internals.

**Files:**
- Create: `services/weather/tests/test_telemetry.py`

**Interfaces:**
- Consumes: `weather.telemetry.setup_telemetry(app) -> None`, `weather.main.app`
- Produces: nothing later tasks depend on

**Pitfall — read before writing:** `trace.set_tracer_provider()` and `metrics.set_meter_provider()` install *process-global* singletons. Calling them twice in one test session logs an override warning and the second call is ignored, which makes tests order-dependent. Every test below patches the provider constructors and setters so no global state is ever installed.

- [ ] **Step 1: Write the failing tests**

Create `services/weather/tests/test_telemetry.py`:

```python
import sys

import pytest
from fastapi.testclient import TestClient

from weather.main import app


def test_telemetry_not_imported_without_endpoint(monkeypatch):
    """The OTel guard: no endpoint set means telemetry is never even imported."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    sys.modules.pop("weather.telemetry", None)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert "weather.telemetry" not in sys.modules


def test_setup_telemetry_called_when_endpoint_set(monkeypatch):
    """With an endpoint set, lifespan wires telemetry up."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    calls = []
    monkeypatch.setattr(
        "weather.telemetry.setup_telemetry", lambda app: calls.append(app)
    )

    with TestClient(app):
        pass

    assert len(calls) == 1


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patch every global-installing SDK call so tests leave no process state."""
    recorded = {"resource": None, "endpoint": None, "fastapi": 0, "httpx": 0}

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            recorded["resource"] = kwargs.get("resource")

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr("weather.telemetry.TracerProvider", FakeProvider)
    monkeypatch.setattr("weather.telemetry.MeterProvider", FakeProvider)
    monkeypatch.setattr("weather.telemetry.BatchSpanProcessor", lambda exporter: None)
    monkeypatch.setattr(
        "weather.telemetry.OTLPSpanExporter",
        lambda endpoint: recorded.__setitem__("endpoint", endpoint),
    )
    monkeypatch.setattr("weather.telemetry.PrometheusMetricReader", lambda: None)
    monkeypatch.setattr("weather.telemetry.trace.set_tracer_provider", lambda p: None)
    monkeypatch.setattr("weather.telemetry.metrics.set_meter_provider", lambda p: None)
    monkeypatch.setattr(
        "weather.telemetry.FastAPIInstrumentor.instrument_app",
        staticmethod(lambda app: recorded.__setitem__("fastapi", recorded["fastapi"] + 1)),
    )

    class FakeHTTPXInstrumentor:
        def instrument(self):
            recorded["httpx"] += 1

    monkeypatch.setattr(
        "weather.telemetry.HTTPXClientInstrumentor", FakeHTTPXInstrumentor
    )
    return recorded


def test_exporter_uses_configured_endpoint(monkeypatch, patched_sdk):
    """The exporter targets whatever OTEL_EXPORTER_OTLP_ENDPOINT says."""
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["endpoint"] == "http://collector.test:4317"


def test_service_name_from_env(monkeypatch, patched_sdk):
    """OTEL_SERVICE_NAME overrides the default resource service.name."""
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "weather-canary")
    setup_telemetry(app)

    assert patched_sdk["resource"].attributes["service.name"] == "weather-canary"


def test_service_name_defaults_to_weather(monkeypatch, patched_sdk):
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    setup_telemetry(app)

    assert patched_sdk["resource"].attributes["service.name"] == "weather"


def test_fastapi_and_httpx_instrumentation_attached(monkeypatch, patched_sdk):
    """Deleting either instrumentation line is silent today. This catches it."""
    from weather.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["fastapi"] == 1
    assert patched_sdk["httpx"] == 1
```

- [ ] **Step 2: Run the tests**

```bash
cd services/weather && uv run pytest tests/test_telemetry.py -v
```

Expected: PASS, 6 tests. `telemetry.py` moves from 0% to roughly 100%.

These tests pass against the existing implementation — `telemetry.py` is correct, it was merely unexercised. The value is regression protection, so there is no red phase here. If any test fails, that is a real defect in `telemetry.py`; fix the source, not the test.

- [ ] **Step 3: Confirm the coverage lift**

```bash
cd services/weather && uv run pytest -q
```

Expected: PASS. `weather/telemetry.py` at or near 100%; TOTAL rises from 61% to roughly 82%.

- [ ] **Step 4: Lint and commit**

```bash
cd services/weather && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add services/weather/tests/test_telemetry.py
git commit -m "test(weather): cover OTel guard, resource attrs, instrumentation wiring"
```

---

## Task 4: weather parser property tests and error paths

`weather/client.py` sits at 68%. The gaps are the fault-injection branches and the failure paths of the Open-Meteo response parser — precisely the code Phase 5B chaos scenarios will drive.

**Files:**
- Create: `services/weather/tests/test_properties.py`
- Modify: `services/weather/pyproject.toml` (add `hypothesis`)

**Interfaces:**
- Consumes: `weather.client.fetch_weather_for_coords(lat, lon, client) -> dict`, `weather.client.fetch_current_weather(location, client) -> dict`, `weather.client.WEATHER_URL`, `weather.client.GEOCODE_URL`
- Produces: nothing later tasks depend on

- [ ] **Step 1: Add Hypothesis to the dev group**

In `services/weather/pyproject.toml`, add `"hypothesis"` to `[dependency-groups] dev`:

```toml
[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "hypothesis",
    "httpx",
    "respx",
]
```

- [ ] **Step 2: Write the failing tests**

Create `services/weather/tests/test_properties.py`:

```python
import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from weather.client import (
    GEOCODE_URL,
    WEATHER_URL,
    fetch_current_weather,
    fetch_weather_for_coords,
)

# Hypothesis drives many examples per test; respx and the event loop are
# function-scoped, so the function_scoped_fixture health check is suppressed.
SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

REQUIRED_CURRENT_FIELDS = [
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "weather_code",
    "precipitation",
    "time",
]


@SETTINGS
@given(missing=st.sampled_from(REQUIRED_CURRENT_FIELDS))
@respx.mock
async def test_missing_current_field_raises_keyerror(missing):
    """A field dropped by the upstream must fail loudly, not return a partial dict."""
    payload = {
        "current": {
            "temperature_2m": 12.0,
            "relative_humidity_2m": 55,
            "wind_speed_10m": 9.0,
            "weather_code": 3,
            "precipitation": 0.0,
            "time": "2026-09-30T14:00",
        }
    }
    del payload["current"][missing]
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=payload))

    async with httpx.AsyncClient() as client:
        with pytest.raises(KeyError):
            await fetch_weather_for_coords(37.7, -122.4, client)


@SETTINGS
@given(status=st.integers(min_value=400, max_value=599))
@respx.mock
async def test_upstream_error_status_raises(status):
    """Every 4xx/5xx from Open-Meteo surfaces as HTTPStatusError."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(status))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_weather_for_coords(37.7, -122.4, client)


@SETTINGS
@given(
    body=st.one_of(
        st.none(),
        st.lists(st.integers(), max_size=3),
        st.text(max_size=20),
        st.integers(),
    )
)
@respx.mock
async def test_non_object_body_raises_typeerror_or_keyerror(body):
    """A JSON body that is not an object must not produce a silent success."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=body))

    async with httpx.AsyncClient() as client:
        with pytest.raises((TypeError, KeyError)):
            await fetch_weather_for_coords(37.7, -122.4, client)


@SETTINGS
@given(
    temp=st.floats(min_value=-100, max_value=100, allow_nan=False),
    humidity=st.integers(min_value=0, max_value=100),
    wind=st.floats(min_value=0, max_value=500, allow_nan=False),
    code=st.integers(min_value=0, max_value=99),
    precip=st.floats(min_value=0, max_value=1000, allow_nan=False),
)
@respx.mock
async def test_wellformed_payload_always_maps_cleanly(
    temp, humidity, wind, code, precip
):
    """Any structurally valid payload maps to the documented output keys."""
    respx.get(WEATHER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": temp,
                    "relative_humidity_2m": humidity,
                    "wind_speed_10m": wind,
                    "weather_code": code,
                    "precipitation": precip,
                    "time": "2026-09-30T14:00",
                }
            },
        )
    )

    async with httpx.AsyncClient() as client:
        result = await fetch_weather_for_coords(37.7, -122.4, client)

    assert result == {
        "temperature_c": temp,
        "relative_humidity_pct": humidity,
        "wind_speed_kmh": wind,
        "weather_code": code,
        "precipitation_mm": precip,
        "time": "2026-09-30T14:00",
    }


@SETTINGS
@given(location=st.text(min_size=1, max_size=30))
@respx.mock
async def test_empty_geocode_results_raise_valueerror(location):
    """An unknown location is a ValueError, never an IndexError."""
    respx.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Location not found"):
            await fetch_current_weather(location, client)


@respx.mock
async def test_geocode_missing_results_key_raises_valueerror():
    """A payload with no `results` key at all behaves the same as an empty list."""
    respx.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Location not found"):
            await fetch_current_weather("nowhere", client)
```

- [ ] **Step 3: Run the tests**

```bash
cd services/weather && uv run pytest tests/test_properties.py -v
```

Expected: PASS.

If `test_non_object_body_raises_typeerror_or_keyerror` fails for the `st.none()` case, that is a genuine finding: `resp.json()["current"]` on a `None` body raises `TypeError`, which is in the accepted tuple — but confirm the behaviour rather than widening the assertion to bare `Exception`. Never loosen an assertion to make a property test pass; narrow the input domain or fix the source.

- [ ] **Step 4: Write the fault-injection tests**

The fault-injection branches in `_maybe_inject_fault` are the remaining `client.py` gap. Append to `services/weather/tests/test_properties.py`:

```python
@SETTINGS
@given(rate=st.floats(min_value=0.999, max_value=1.0))
@respx.mock
async def test_fault_error_rate_injects_503(monkeypatch, rate):
    """FAULT_UPSTREAM_ERROR_RATE at ~1.0 always raises before the real call."""
    monkeypatch.setenv("FAULT_UPSTREAM_ERROR_RATE", str(rate))
    route = respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json={}))

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await fetch_weather_for_coords(37.7, -122.4, client)

    assert exc.value.response.status_code == 503
    assert not route.called  # fault short-circuits before the upstream request


async def test_fault_latency_delays_call(monkeypatch):
    """FAULT_UPSTREAM_LATENCY_MS sleeps for the configured duration."""
    slept = []
    monkeypatch.setenv("FAULT_UPSTREAM_LATENCY_MS", "250")

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("weather.client.asyncio.sleep", fake_sleep)

    with respx.mock:
        respx.get(WEATHER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "current": {
                        "temperature_2m": 1.0,
                        "relative_humidity_2m": 1,
                        "wind_speed_10m": 1.0,
                        "weather_code": 1,
                        "precipitation": 0.0,
                        "time": "t",
                    }
                },
            )
        )
        async with httpx.AsyncClient() as client:
            await fetch_weather_for_coords(37.7, -122.4, client)

    assert slept == [0.25]


async def test_no_fault_env_vars_is_inert(monkeypatch):
    """With no FAULT_* vars set, the injector must not sleep or raise."""
    monkeypatch.delenv("FAULT_UPSTREAM_LATENCY_MS", raising=False)
    monkeypatch.delenv("FAULT_UPSTREAM_ERROR_RATE", raising=False)

    with respx.mock:
        route = respx.get(WEATHER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "current": {
                        "temperature_2m": 1.0,
                        "relative_humidity_2m": 1,
                        "wind_speed_10m": 1.0,
                        "weather_code": 1,
                        "precipitation": 0.0,
                        "time": "t",
                    }
                },
            )
        )
        async with httpx.AsyncClient() as client:
            await fetch_weather_for_coords(37.7, -122.4, client)

    assert route.called
```

- [ ] **Step 5: Run the full weather suite**

```bash
cd services/weather && uv run pytest -q
```

Expected: PASS. `weather/client.py` at or above 95%; TOTAL at or above 90%.

- [ ] **Step 6: Lint and commit**

```bash
cd services/weather && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add services/weather/tests/test_properties.py services/weather/pyproject.toml
git commit -m "test(weather): property-based parser and fault-injection coverage"
```

---

## Task 5: weather OpenAPI snapshot contract

The single highest-value addition in this phase. Catches *"breaking API contract (response field renamed)"* — a Phase 5C fault-catalog entry — with no consumer required.

**Files:**
- Create: `contracts/openapi/weather.json`
- Create: `services/weather/tests/test_contract.py`

**Interfaces:**
- Consumes: `weather.main.app`
- Produces: the `contracts/openapi/` directory convention, reused by Task 11

- [ ] **Step 1: Write the failing test**

Create `services/weather/tests/test_contract.py`:

```python
import json
from pathlib import Path

from weather.main import app

CONTRACT = (
    Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "weather.json"
)

REGENERATE_HINT = (
    "The service's OpenAPI surface changed.\n"
    "If the change is intentional, regenerate the snapshot:\n"
    "  cd services/weather && uv run python -c "
    "\"import json,pathlib; from weather.main import app; "
    "pathlib.Path('../../contracts/openapi/weather.json').write_text("
    "json.dumps(app.openapi(), indent=2, sort_keys=True) + '\\n')\"\n"
    "and include it in the same PR so the surface change is explicit in review."
)


def test_openapi_snapshot_matches_committed_contract():
    committed = json.loads(CONTRACT.read_text())
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert live == committed, REGENERATE_HINT


def test_documented_paths_are_present():
    """Guards against a route being deleted outright."""
    paths = set(app.openapi()["paths"])
    assert {
        "/health",
        "/metrics",
        "/weather/stadiums",
        "/weather/stadiums/{stadium_id}",
    } <= paths
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd services/weather && uv run pytest tests/test_contract.py -v
```

Expected: FAIL with `FileNotFoundError` — `contracts/openapi/weather.json` does not exist yet.

- [ ] **Step 3: Generate the snapshot**

```bash
mkdir -p contracts/openapi
cd services/weather && uv run python -c "import json,pathlib; from weather.main import app; pathlib.Path('../../contracts/openapi/weather.json').write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + '\n')"
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd services/weather && uv run pytest tests/test_contract.py -v
```

Expected: PASS, 2 tests.

- [ ] **Step 5: Prove the test actually detects divergence**

A snapshot test that cannot fail is worthless. Verify it:

```bash
cd services/weather
python - <<'PY'
import json, pathlib
p = pathlib.Path('../../contracts/openapi/weather.json')
d = json.loads(p.read_text())
d['paths']['/deliberately-broken'] = {}
p.write_text(json.dumps(d, indent=2, sort_keys=True) + '\n')
PY
uv run pytest tests/test_contract.py::test_openapi_snapshot_matches_committed_contract -q
```

Expected: FAIL, printing the regeneration hint. Then restore:

```bash
cd services/weather && uv run python -c "import json,pathlib; from weather.main import app; pathlib.Path('../../contracts/openapi/weather.json').write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + '\n')"
uv run pytest tests/test_contract.py -q
```

Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
cd services/weather && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add contracts/openapi/weather.json services/weather/tests/test_contract.py
git commit -m "test(weather): commit OpenAPI snapshot with divergence detection"
```

---

## Task 6: weather integration suite

Exercises the whole app over real HTTP with no mocked internals — only the external Open-Meteo boundary is stubbed.

**Files:**
- Create: `services/weather/tests/integration/__init__.py`
- Create: `services/weather/tests/integration/test_app.py`

**Interfaces:**
- Consumes: `weather.main.app`, `weather.stadiums.STADIUMS`
- Produces: nothing later tasks depend on

- [ ] **Step 1: Create the package marker**

```bash
mkdir -p services/weather/tests/integration
touch services/weather/tests/integration/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `services/weather/tests/integration/test_app.py`:

```python
import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from weather.client import WEATHER_URL
from weather.main import app
from weather.stadiums import STADIUMS

VALID_CURRENT = {
    "current": {
        "temperature_2m": 18.0,
        "relative_humidity_2m": 62,
        "wind_speed_10m": 11.0,
        "weather_code": 1,
        "precipitation": 0.0,
        "time": "2026-09-30T14:00",
    }
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@respx.mock
def test_all_stadiums_returns_every_stadium(client):
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=VALID_CURRENT))

    body = client.get("/weather/stadiums").json()

    assert body["count"] == len(STADIUMS)
    assert len(body["stadiums"]) == len(STADIUMS)
    assert all(s["weather"] is not None for s in body["stadiums"])


@respx.mock
def test_all_stadiums_degrades_per_stadium_on_upstream_error(client):
    """One bad upstream must not fail the whole collection — weather goes null."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(503))

    body = client.get("/weather/stadiums").json()

    assert body["count"] == len(STADIUMS)
    assert all(s["weather"] is None for s in body["stadiums"])


@respx.mock
def test_all_stadiums_survives_upstream_timeout(client):
    respx.get(WEATHER_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    body = client.get("/weather/stadiums").json()

    assert body["count"] == len(STADIUMS)
    assert all(s["weather"] is None for s in body["stadiums"])


@respx.mock
def test_single_stadium_returns_502_on_upstream_error(client):
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(500))
    stadium_id = next(iter(STADIUMS))

    resp = client.get(f"/weather/stadiums/{stadium_id}")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Weather API error"


@respx.mock
def test_single_stadium_returns_502_when_upstream_unreachable(client):
    respx.get(WEATHER_URL).mock(side_effect=httpx.ConnectError("no route"))
    stadium_id = next(iter(STADIUMS))

    resp = client.get(f"/weather/stadiums/{stadium_id}")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Weather API unreachable"


def test_unknown_stadium_returns_404(client):
    resp = client.get("/weather/stadiums/not-a-real-stadium")

    assert resp.status_code == 404
    assert "not-a-real-stadium" in resp.json()["detail"]


@respx.mock
def test_concurrent_requests_are_independent(client):
    """Twenty simultaneous requests must all succeed with identical bodies."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=VALID_CURRENT))
    stadium_id = next(iter(STADIUMS))

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            return await asyncio.gather(
                *(ac.get(f"/weather/stadiums/{stadium_id}") for _ in range(20))
            )

    responses = asyncio.run(hammer())

    assert all(r.status_code == 200 for r in responses)
    assert len({r.text for r in responses}) == 1


def test_health_and_metrics_are_live(client):
    assert client.get("/health").json() == {"status": "ok"}

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
```

- [ ] **Step 3: Run the tests**

```bash
cd services/weather && uv run pytest tests/integration/ -v
```

Expected: PASS, 8 tests.

- [ ] **Step 4: Confirm the whole suite and coverage**

```bash
cd services/weather && uv run pytest -q
```

Expected: PASS. `weather` TOTAL at or above 90%, comfortably over the 80% gate arriving in Task 13.

- [ ] **Step 5: Lint and commit**

```bash
cd services/weather && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add services/weather/tests/integration/
git commit -m "test(weather): real-HTTP integration suite"
```

---

## Task 7: player-projections telemetry tests

Same shape as Task 3, against the `player_projections` package. The code is repeated deliberately — this task may be executed by someone who has not read Task 3.

**Files:**
- Create: `services/player-projections/tests/test_telemetry.py`

**Interfaces:**
- Consumes: `player_projections.telemetry.setup_telemetry(app) -> None`, `player_projections.main.app`
- Produces: nothing later tasks depend on

**Pitfall:** as in Task 3, `trace.set_tracer_provider()` and `metrics.set_meter_provider()` install process-global singletons. Every test below patches them.

- [ ] **Step 1: Confirm the module mirrors weather's**

```bash
diff services/weather/weather/telemetry.py services/player-projections/player_projections/telemetry.py
```

Expected: differs only in the default `service.name` (`"weather"` vs `"player-projections"`). If it differs structurally, adapt the patch targets below to match.

- [ ] **Step 2: Write the tests**

Create `services/player-projections/tests/test_telemetry.py`:

```python
import sys

import pytest
from fastapi.testclient import TestClient

from player_projections.main import app


def test_telemetry_not_imported_without_endpoint(monkeypatch):
    """The OTel guard: no endpoint set means telemetry is never even imported."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    sys.modules.pop("player_projections.telemetry", None)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert "player_projections.telemetry" not in sys.modules


def test_setup_telemetry_called_when_endpoint_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    calls = []
    monkeypatch.setattr(
        "player_projections.telemetry.setup_telemetry", lambda app: calls.append(app)
    )

    with TestClient(app):
        pass

    assert len(calls) == 1


@pytest.fixture
def patched_sdk(monkeypatch):
    """Patch every global-installing SDK call so tests leave no process state."""
    recorded = {"resource": None, "endpoint": None, "fastapi": 0, "httpx": 0}
    mod = "player_projections.telemetry"

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            recorded["resource"] = kwargs.get("resource")

        def add_span_processor(self, processor):
            pass

    monkeypatch.setattr(f"{mod}.TracerProvider", FakeProvider)
    monkeypatch.setattr(f"{mod}.MeterProvider", FakeProvider)
    monkeypatch.setattr(f"{mod}.BatchSpanProcessor", lambda exporter: None)
    monkeypatch.setattr(
        f"{mod}.OTLPSpanExporter",
        lambda endpoint: recorded.__setitem__("endpoint", endpoint),
    )
    monkeypatch.setattr(f"{mod}.PrometheusMetricReader", lambda: None)
    monkeypatch.setattr(f"{mod}.trace.set_tracer_provider", lambda p: None)
    monkeypatch.setattr(f"{mod}.metrics.set_meter_provider", lambda p: None)
    monkeypatch.setattr(
        f"{mod}.FastAPIInstrumentor.instrument_app",
        staticmethod(
            lambda app: recorded.__setitem__("fastapi", recorded["fastapi"] + 1)
        ),
    )

    class FakeHTTPXInstrumentor:
        def instrument(self):
            recorded["httpx"] += 1

    monkeypatch.setattr(f"{mod}.HTTPXClientInstrumentor", FakeHTTPXInstrumentor)
    return recorded


def test_exporter_uses_configured_endpoint(monkeypatch, patched_sdk):
    from player_projections.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["endpoint"] == "http://collector.test:4317"


def test_service_name_defaults_to_player_projections(monkeypatch, patched_sdk):
    from player_projections.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    setup_telemetry(app)

    assert (
        patched_sdk["resource"].attributes["service.name"] == "player-projections"
    )


def test_fastapi_and_httpx_instrumentation_attached(monkeypatch, patched_sdk):
    from player_projections.telemetry import setup_telemetry

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317")
    setup_telemetry(app)

    assert patched_sdk["fastapi"] == 1
    assert patched_sdk["httpx"] == 1
```

- [ ] **Step 3: Run the tests**

```bash
cd services/player-projections && uv run pytest tests/test_telemetry.py -v
```

Expected: PASS, 5 tests. `telemetry.py` moves from 0% to roughly 100%; TOTAL rises from 42% to roughly 68%.

If `test_service_name_defaults_to_player_projections` fails, read the actual default in `player_projections/telemetry.py` and assert that value — do not change the source to match the test.

- [ ] **Step 4: Lint and commit**

```bash
cd services/player-projections && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add services/player-projections/tests/test_telemetry.py
git commit -m "test(player-projections): cover OTel guard and instrumentation wiring"
```

---

## Task 8: player-projections poll loop coverage

`main.py` is at 51%. The uncovered half is `_poll_loop` — the background task that is the service's entire reason for existing, and which currently swallows every exception into a boolean.

**Files:**
- Create: `services/player-projections/tests/test_poll_loop.py`

**Interfaces:**
- Consumes: `player_projections.main._poll_loop() -> None`, `player_projections.main._state`, `player_projections.main._now_iso() -> str`
- Produces: the `reset_state` fixture pattern reused in Tasks 10 and 11

**Pitfall:** `_state` is a module-level dict shared across tests. Every test must reset it, or results depend on execution order.

- [ ] **Step 1: Write the tests**

Create `services/player-projections/tests/test_poll_loop.py`:

```python
import asyncio

import pytest

from player_projections import main


@pytest.fixture(autouse=True)
def reset_state():
    """_state is module-global; reset it around every test."""
    main._state["projections"] = {}
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False
    yield
    main._state["projections"] = {}
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False


@pytest.fixture
def one_iteration(monkeypatch):
    """Make the infinite poll loop run exactly one pass, then stop."""

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)


async def test_stub_mode_returns_immediately_without_polling(monkeypatch):
    """No PLAYER_DATA_URL means the loop exits without ever calling upstream."""
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    called = []
    monkeypatch.setattr(
        main, "fetch_projections", lambda url: called.append(url)
    )

    await main._poll_loop()

    assert called == []
    assert main._state["upstream_healthy"] is False


async def test_successful_poll_populates_cache(monkeypatch, one_iteration):
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")

    async def fake_fetch(url):
        return [
            {"id": "p_1", "name": "A", "pos": "WR", "rank": 1},
            {"id": "p_2", "name": "B", "pos": "RB", "rank": 2},
        ]

    monkeypatch.setattr(main, "fetch_projections", fake_fetch)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert set(main._state["projections"]) == {"p_1", "p_2"}
    assert main._state["upstream_healthy"] is True
    assert main._state["last_updated"] is not None


async def test_upstream_failure_marks_unhealthy(monkeypatch, one_iteration):
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")

    async def boom(url):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(main, "fetch_projections", boom)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert main._state["upstream_healthy"] is False
    assert main._state["projections"] == {}


async def test_failure_after_success_retains_last_good_data(
    monkeypatch, one_iteration
):
    """A later failure must not wipe the cache — stale data beats no data."""
    main._state["projections"] = {"p_1": {"id": "p_1"}}
    main._state["upstream_healthy"] = True
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")

    async def boom(url):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(main, "fetch_projections", boom)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert main._state["projections"] == {"p_1": {"id": "p_1"}}
    assert main._state["upstream_healthy"] is False


async def test_poll_interval_read_from_env(monkeypatch):
    """POLL_INTERVAL_SECONDS controls the sleep duration."""
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "42")
    slept = []

    async def capture(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", capture)

    async def fake_fetch(url):
        return []

    monkeypatch.setattr(main, "fetch_projections", fake_fetch)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert slept == [42]


def test_now_iso_is_utc_and_parseable():
    from datetime import datetime

    value = main._now_iso()
    parsed = datetime.fromisoformat(value)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
```

- [ ] **Step 2: Run the tests**

```bash
cd services/player-projections && uv run pytest tests/test_poll_loop.py -v
```

Expected: PASS, 6 tests.

`test_stub_mode_returns_immediately_without_polling` may need `fetch_projections` patched as an async callable if the implementation awaits it before the URL check — read `main._poll_loop` and confirm the `if not url: return` guard comes first. It does in the current source.

- [ ] **Step 3: Check the coverage lift**

```bash
cd services/player-projections && uv run pytest -q
```

Expected: PASS. `main.py` at or above 85%; TOTAL at or above 88%.

- [ ] **Step 4: Lint and commit**

```bash
cd services/player-projections && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add services/player-projections/tests/test_poll_loop.py
git commit -m "test(player-projections): cover the background poll loop"
```

---

## Task 9: player-data snapshot contracts

Three JSON Schemas — one per scoring format — plus fixtures and validation tests. These encode the intended shape of the documents `player-data` will publish to S3.

**Files:**
- Create: `contracts/player-data/standard.v1.schema.json`
- Create: `contracts/player-data/half-ppr.v1.schema.json`
- Create: `contracts/player-data/ppr.v1.schema.json`
- Create: `contracts/player-data/fixtures/ppr-valid.json`
- Create: `contracts/player-data/fixtures/ppr-missing-id.json`
- Create: `contracts/player-data/README.md`
- Modify: `services/player-projections/pyproject.toml` (add `jsonschema`)

**Interfaces:**
- Consumes: nothing
- Produces: `contracts/player-data/<format>.v1.schema.json` — loaded by Task 10's tests

- [ ] **Step 1: Add `jsonschema` to the dev group**

In `services/player-projections/pyproject.toml`, add `"jsonschema"` and `"hypothesis"` to `[dependency-groups] dev`:

```toml
[dependency-groups]
dev = [
    "ruff",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "hypothesis",
    "jsonschema",
    "httpx",
    "respx",
]
```

- [ ] **Step 2: Write the PPR schema**

Create `contracts/player-data/ppr.v1.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://foundry.internal/contracts/player-data/ppr.v1.schema.json",
  "title": "player-data weekly projections snapshot (PPR scoring)",
  "type": "object",
  "required": ["format", "season", "week", "generated_at", "players"],
  "additionalProperties": false,
  "properties": {
    "format": { "const": "ppr" },
    "season": { "type": "integer", "minimum": 2020, "maximum": 2100 },
    "week": { "type": "integer", "minimum": 1, "maximum": 22 },
    "generated_at": { "type": "string", "format": "date-time" },
    "players": {
      "type": "array",
      "items": { "$ref": "#/$defs/player" }
    }
  },
  "$defs": {
    "player": {
      "type": "object",
      "required": ["id", "name", "pos", "team", "rank", "proj_points"],
      "additionalProperties": false,
      "properties": {
        "id": { "type": "string", "pattern": "^p_[a-z0-9]+$" },
        "name": { "type": "string", "minLength": 1 },
        "pos": { "enum": ["QB", "RB", "WR", "TE", "K", "DST"] },
        "team": { "type": "string", "minLength": 2, "maxLength": 4 },
        "rank": { "type": "integer", "minimum": 1 },
        "proj_points": { "$ref": "#/$defs/spread" }
      }
    },
    "spread": {
      "type": "object",
      "required": ["floor", "expected", "ceiling"],
      "additionalProperties": false,
      "properties": {
        "floor": { "type": "number", "minimum": 0 },
        "expected": { "type": "number", "minimum": 0 },
        "ceiling": { "type": "number", "minimum": 0 }
      }
    }
  }
}
```

- [ ] **Step 3: Write the standard and half-PPR schemas**

Create `contracts/player-data/standard.v1.schema.json` and `contracts/player-data/half-ppr.v1.schema.json` as byte-identical copies of the PPR schema with exactly two changes each:

- `"$id"` → `.../standard.v1.schema.json` / `.../half-ppr.v1.schema.json`
- `"format": { "const": "ppr" }` → `{ "const": "standard" }` / `{ "const": "half-ppr" }`
- the `"title"` scoring name

The schemas are otherwise identical by design: the scoring format changes the *values* of `rank` and `proj_points`, not the shape.

- [ ] **Step 4: Write the fixtures**

Create `contracts/player-data/fixtures/ppr-valid.json`:

```json
{
  "format": "ppr",
  "season": 2026,
  "week": 5,
  "generated_at": "2026-09-30T14:00:00Z",
  "players": [
    {
      "id": "p_8f3a21",
      "name": "Deebo Samuel",
      "pos": "WR",
      "team": "SF",
      "rank": 3,
      "proj_points": { "floor": 5.2, "expected": 12.4, "ceiling": 20.1 }
    },
    {
      "id": "p_1c9e04",
      "name": "Christian McCaffrey",
      "pos": "RB",
      "team": "SF",
      "rank": 1,
      "proj_points": { "floor": 11.0, "expected": 21.7, "ceiling": 33.5 }
    }
  ]
}
```

Create `contracts/player-data/fixtures/ppr-missing-id.json` — the same document with the `id` key removed from the first player. This is the fixture Task 10 uses to prove the consumer degrades safely.

- [ ] **Step 5: Document the contract directory**

Create `contracts/player-data/README.md`:

```markdown
# player-data snapshot contracts

`player-data` publishes one JSON document per scoring format per week to S3.
`player-projections` polls the document matching the requested format.

| File | Scoring |
|---|---|
| `standard.v1.schema.json` | Standard (no reception points) |
| `half-ppr.v1.schema.json` | 0.5 points per reception |
| `ppr.v1.schema.json` | 1 point per reception |

The three schemas are structurally identical. Scoring format changes the
*values* of `rank` and `proj_points`, not the shape of the document.

## Direction

These contracts are **provider-driven**: the schema is authoritative and both
sides conform to it. See [ADR 0002](../../docs/adr/0002-provider-driven-contracts.md)
for why this project does not use consumer-driven contract testing (Pact), and
the condition under which that decision should be revisited.

## When `player-data` is built

It must validate its output against these files in its own CI before
publishing. Until then the schemas are validated against the fixtures in
`fixtures/` only — they encode an intended shape, not an observed one.

## Versioning

The `.v1.` in each filename is the contract version. A backward-compatible
addition (a new optional field) may amend v1 in place. Any change that would
break an existing consumer — removing a field, narrowing a type, changing an
enum — requires a new `.v2.` file published alongside v1.
```

- [ ] **Step 6: Verify the schemas are valid JSON Schema**

```bash
cd services/player-projections
uv run python - <<'PY'
import json, pathlib
from jsonschema import Draft202012Validator
root = pathlib.Path('../../contracts/player-data')
for f in sorted(root.glob('*.schema.json')):
    schema = json.loads(f.read_text())
    Draft202012Validator.check_schema(schema)
    print('ok', f.name)
valid = json.loads((root / 'fixtures' / 'ppr-valid.json').read_text())
Draft202012Validator(json.loads((root / 'ppr.v1.schema.json').read_text())).validate(valid)
print('fixture validates')
PY
```

Expected: three `ok` lines and `fixture validates`.

- [ ] **Step 7: Commit**

```bash
git add contracts/player-data/ services/player-projections/pyproject.toml
git commit -m "feat(contracts): add player-data snapshot schemas for all three scoring formats"
```

---

## Task 10: player-projections contract and property tests

Validates the consumer against the schemas from Task 9 and hardens the parser. **This task changes source code** — the property tests expose two real robustness defects.

**Files:**
- Create: `services/player-projections/tests/test_contract.py`
- Create: `services/player-projections/tests/test_properties.py`
- Modify: `services/player-projections/player_projections/client.py`
- Modify: `services/player-projections/player_projections/main.py`

**Interfaces:**
- Consumes: `contracts/player-data/*.v1.schema.json` (Task 9), `player_projections.client.fetch_projections(url) -> list[dict]`, `player_projections.main._poll_loop`, `_state`
- Produces: `player_projections.client.MalformedSnapshotError` — a subclass of `ValueError`

- [ ] **Step 1: Write the failing contract test**

Create `services/player-projections/tests/test_contract.py`:

```python
import json
from pathlib import Path

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from player_projections.client import fetch_projections

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "player-data"
FORMATS = ["standard", "half-ppr", "ppr"]


@pytest.mark.parametrize("fmt", FORMATS)
def test_schema_is_valid_json_schema(fmt):
    schema = json.loads((CONTRACTS / f"{fmt}.v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("fmt", FORMATS)
def test_schema_declares_its_own_format(fmt):
    """Each schema pins `format` to its own scoring type — files can't be mixed up."""
    schema = json.loads((CONTRACTS / f"{fmt}.v1.schema.json").read_text())
    assert schema["properties"]["format"]["const"] == fmt


def test_fixture_validates_against_ppr_schema():
    schema = json.loads((CONTRACTS / "ppr.v1.schema.json").read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    Draft202012Validator(schema).validate(fixture)


def test_schemas_are_structurally_identical_across_formats():
    """Scoring changes values, not shape. Divergence here is a contract bug."""
    defs = []
    for fmt in FORMATS:
        schema = json.loads((CONTRACTS / f"{fmt}.v1.schema.json").read_text())
        defs.append(schema["$defs"])
    assert defs[0] == defs[1] == defs[2]


@respx.mock
async def test_consumer_parses_a_schema_valid_snapshot():
    """The real contract assertion: a schema-valid document parses cleanly."""
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    url = "https://example.test/ppr.json"
    respx.get(url).mock(return_value=httpx.Response(200, json=fixture))

    players = await fetch_projections(url)

    assert len(players) == 2
    assert {p["id"] for p in players} == {"p_8f3a21", "p_1c9e04"}
    assert players[0]["proj_points"]["expected"] == 12.4
```

- [ ] **Step 2: Run it**

```bash
cd services/player-projections && uv run pytest tests/test_contract.py -v
```

Expected: PASS, 9 tests.

- [ ] **Step 3: Write the failing property tests**

Create `services/player-projections/tests/test_properties.py`:

```python
import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from player_projections import main
from player_projections.client import MalformedSnapshotError, fetch_projections

SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

URL = "https://example.test/ppr.json"


@SETTINGS
@given(body=st.one_of(st.none(), st.lists(st.integers(), max_size=3), st.integers()))
@respx.mock
async def test_non_object_snapshot_raises_typed_error(body):
    """A snapshot that isn't a JSON object must raise a typed error, not AttributeError."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(MalformedSnapshotError):
        await fetch_projections(URL)


@respx.mock
async def test_missing_players_key_returns_empty_list():
    """An object with no `players` key is an empty snapshot, not an error."""
    respx.get(URL).mock(return_value=httpx.Response(200, json={"format": "ppr"}))

    assert await fetch_projections(URL) == []


@SETTINGS
@given(status=st.integers(min_value=400, max_value=599))
@respx.mock
async def test_error_status_raises_http_error(status):
    respx.get(URL).mock(return_value=httpx.Response(status))

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_projections(URL)


@SETTINGS
@given(
    good=st.integers(min_value=1, max_value=5),
    bad=st.integers(min_value=1, max_value=5),
)
async def test_records_without_id_are_skipped_not_fatal(monkeypatch, good, bad):
    """One malformed record must not discard the whole batch."""
    main._state["projections"] = {}
    main._state["upstream_healthy"] = False

    players = [{"id": f"p_{i}", "name": "ok"} for i in range(good)]
    players += [{"name": "no id here"} for _ in range(bad)]

    async def fake_fetch(url):
        return players

    async def stop(_s):
        raise ImportError("stop")  # sentinel distinct from any real failure

    monkeypatch.setenv("PLAYER_DATA_URL", URL)
    monkeypatch.setattr(main, "fetch_projections", fake_fetch)
    monkeypatch.setattr(main.asyncio, "sleep", stop)

    with pytest.raises(ImportError):
        await main._poll_loop()

    assert len(main._state["projections"]) == good
    assert main._state["upstream_healthy"] is True


@SETTINGS
@given(size=st.integers(min_value=500, max_value=2000))
@respx.mock
async def test_large_snapshot_parses(size):
    """A full-league snapshot is ~500-1000 players. Parsing must not degrade."""
    payload = {
        "format": "ppr",
        "players": [{"id": f"p_{i}", "rank": i + 1} for i in range(size)],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=payload))

    players = await fetch_projections(URL)

    assert len(players) == size
```

- [ ] **Step 4: Run it to verify it fails**

```bash
cd services/player-projections && uv run pytest tests/test_properties.py -v
```

Expected: FAIL — `ImportError: cannot import name 'MalformedSnapshotError'`, and `test_records_without_id_are_skipped_not_fatal` fails because `{p["id"]: p for p in players}` raises `KeyError`, which the bare `except Exception` converts into `upstream_healthy = False` with an empty cache.

Both failures are real defects, not test artifacts: one bad record currently discards every good record in the batch and reports the upstream as unhealthy.

- [ ] **Step 5: Add the typed error to the client**

Replace `services/player-projections/player_projections/client.py`:

```python
import httpx


class MalformedSnapshotError(ValueError):
    """The upstream snapshot was not a JSON object with the expected shape."""


async def fetch_projections(url: str) -> list[dict]:
    """Fetch player projections from the S3 file written by the player-data backend.

    Raises:
        httpx.HTTPStatusError: the upstream returned a 4xx or 5xx.
        MalformedSnapshotError: the body was not a JSON object.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        body = response.json()

    if not isinstance(body, dict):
        raise MalformedSnapshotError(
            f"expected a JSON object at {url}, got {type(body).__name__}"
        )

    players = body.get("players", [])
    if not isinstance(players, list):
        raise MalformedSnapshotError(
            f"expected `players` to be a list, got {type(players).__name__}"
        )
    return players
```

- [ ] **Step 6: Make the poll loop skip malformed records**

In `services/player-projections/player_projections/main.py`, replace the cache-building line inside `_poll_loop`:

```python
            players = await fetch_projections(url)
            _state["projections"] = {
                p["id"]: p
                for p in players
                if isinstance(p, dict) and isinstance(p.get("id"), str)
            }
            _state["last_updated"] = _now_iso()
            _state["upstream_healthy"] = True
```

A record without a usable `id` cannot be cached or served — it is dropped. The batch still lands and the upstream is still healthy, which is the correct reading: the *upstream* responded fine, a *record* was bad.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
cd services/player-projections && uv run pytest tests/test_properties.py -v
```

Expected: PASS, 5 tests.

- [ ] **Step 8: Confirm nothing regressed**

```bash
cd services/player-projections && uv run pytest -q
```

Expected: PASS, whole suite. TOTAL at or above 90%.

- [ ] **Step 9: Lint and commit**

```bash
cd services/player-projections && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add services/player-projections/
git commit -m "fix(player-projections): skip malformed records, raise typed snapshot errors"
```

---

## Task 11: player-projections OpenAPI snapshot, integration suite, and foundry-cli entrypoint tests

Closes the last three coverage gaps in one task — they share a verification cycle and none is independently rejectable.

**Files:**
- Create: `contracts/openapi/player-projections.json`
- Create: `services/player-projections/tests/integration/__init__.py`
- Create: `services/player-projections/tests/integration/test_app.py`
- Create: `services/foundry-cli/tests/test_cli_entrypoint.py`
- Modify: `services/player-projections/tests/test_contract.py` (append the OpenAPI tests)

**Interfaces:**
- Consumes: `player_projections.main.app`, `_state`; `foundry.cli.main`
- Produces: nothing later tasks depend on

- [ ] **Step 1: Append the OpenAPI snapshot tests**

Add to `services/player-projections/tests/test_contract.py`. **The new import goes at
the top of the file with the others** — ruff lints `E` (which includes `E402`,
module-level import not at top) and `I` (isort), so a mid-file import fails the
lint step:

```python
# add to the existing import block at the top of the file
from player_projections.main import app
```

```python
# append below the existing tests
OPENAPI = Path(__file__).resolve().parents[3] / "contracts" / "openapi"

REGENERATE_HINT = (
    "The service's OpenAPI surface changed.\n"
    "If the change is intentional, regenerate the snapshot:\n"
    "  cd services/player-projections && uv run python -c "
    "\"import json,pathlib; from player_projections.main import app; "
    "pathlib.Path('../../contracts/openapi/player-projections.json').write_text("
    "json.dumps(app.openapi(), indent=2, sort_keys=True) + '\\n')\"\n"
    "and include it in the same PR so the surface change is explicit in review."
)


def test_openapi_snapshot_matches_committed_contract():
    committed = json.loads((OPENAPI / "player-projections.json").read_text())
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert live == committed, REGENERATE_HINT


def test_documented_paths_are_present():
    paths = set(app.openapi()["paths"])
    assert {
        "/health",
        "/metrics",
        "/projections",
        "/projections/{player_id}",
    } <= paths
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd services/player-projections && uv run pytest tests/test_contract.py -v
```

Expected: FAIL with `FileNotFoundError` for `contracts/openapi/player-projections.json`.

- [ ] **Step 3: Generate the snapshot and verify it passes**

```bash
cd services/player-projections && uv run python -c "import json,pathlib; from player_projections.main import app; pathlib.Path('../../contracts/openapi/player-projections.json').write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + '\n')"
uv run pytest tests/test_contract.py -v
```

Expected: PASS.

- [ ] **Step 4: Write the integration suite**

```bash
mkdir -p services/player-projections/tests/integration
touch services/player-projections/tests/integration/__init__.py
```

Create `services/player-projections/tests/integration/test_app.py`:

```python
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from player_projections import main
from player_projections.main import app


@pytest.fixture(autouse=True)
def stub_mode(monkeypatch):
    """No upstream configured — the deployed default today."""
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    main._state["projections"] = {}
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False
    yield
    main._state["projections"] = {}
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_stub_mode_returns_empty_projections(client):
    """The documented stub-mode contract from CLAUDE.md."""
    body = client.get("/projections").json()

    assert body["projections"] == []
    assert body["count"] == 0
    assert body["upstream_healthy"] is False
    assert body["last_updated"] is None


def test_unknown_player_returns_404(client):
    resp = client.get("/projections/p_does_not_exist")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Player not found"


def test_populated_cache_is_served(client):
    main._state["projections"] = {
        "p_8f3a21": {
            "id": "p_8f3a21",
            "name": "Deebo Samuel",
            "pos": "WR",
            "rank": 3,
            "proj_points": {"floor": 5.2, "expected": 12.4, "ceiling": 20.1},
        }
    }
    main._state["upstream_healthy"] = True

    listing = client.get("/projections").json()
    single = client.get("/projections/p_8f3a21").json()

    assert listing["count"] == 1
    assert listing["upstream_healthy"] is True
    assert single["name"] == "Deebo Samuel"
    assert single["proj_points"]["ceiling"] == 20.1


def test_concurrent_reads_are_consistent():
    """Fifty simultaneous reads against a populated cache return identical bodies."""
    main._state["projections"] = {
        f"p_{i}": {"id": f"p_{i}", "rank": i + 1} for i in range(100)
    }

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            return await asyncio.gather(
                *(ac.get("/projections") for _ in range(50))
            )

    responses = asyncio.run(hammer())

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["count"] == 100 for r in responses)
    assert len({r.text for r in responses}) == 1


def test_health_and_metrics_are_live(client):
    assert client.get("/health").json() == {"status": "ok"}

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
```

- [ ] **Step 5: Run it**

```bash
cd services/player-projections && uv run pytest -q
```

Expected: PASS, whole suite. TOTAL at or above 92%.

- [ ] **Step 6: Cover the foundry-cli entrypoint**

`foundry/cli.py` is the only file below the gate in that package (0%, 31 statements).

Its shape, for reference: `main(argv: list[str] | None = None) -> int` builds an
`argparse` parser with a single **required** `triage` subcommand, then imports
`foundry.triage.pipeline.detect` and `foundry.triage.narrator.narrate` *inside*
the function body. Patch targets must therefore be the source modules, not
`foundry.cli` attributes.

Create `services/foundry-cli/tests/test_cli_entrypoint.py`:

```python
import json

import pytest

from foundry import cli


class FakeBundle:
    def to_dict(self):
        return {"service": "weather", "suspects": []}


def test_no_args_exits_two_with_usage(capsys):
    """A required subparser means bare invocation is an argparse error."""
    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_help_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_command_exits_two():
    with pytest.raises(SystemExit) as exc:
        cli.main(["definitely-not-a-command"])

    assert exc.value.code == 2


def test_triage_requires_service_flag():
    with pytest.raises(SystemExit) as exc:
        cli.main(["triage"])

    assert exc.value.code == 2


def test_parser_defaults():
    args = cli.build_parser().parse_args(["triage", "--service", "weather"])

    assert args.command == "triage"
    assert args.service == "weather"
    assert args.endpoint is None
    assert args.incident == ""
    assert args.prometheus_url == "http://localhost:9090"
    assert args.gitops_dir == "infra/gitops"
    assert args.json is False


def test_triage_json_flag_prints_bundle_and_skips_narrator(monkeypatch, capsys):
    """--json emits only the EvidenceBundle; the LLM narrator is never called."""
    narrated = []
    monkeypatch.setattr(
        "foundry.triage.pipeline.detect", lambda **kwargs: FakeBundle()
    )
    monkeypatch.setattr(
        "foundry.triage.narrator.narrate", lambda b: narrated.append(b) or "x"
    )

    code = cli.main(["triage", "--service", "weather", "--json"])

    assert code == 0
    assert narrated == []
    assert json.loads(capsys.readouterr().out) == {
        "service": "weather",
        "suspects": [],
    }


def test_triage_without_json_prints_narrative(monkeypatch, capsys):
    monkeypatch.setattr(
        "foundry.triage.pipeline.detect", lambda **kwargs: FakeBundle()
    )
    monkeypatch.setattr(
        "foundry.triage.narrator.narrate", lambda b: "the deploy did it"
    )

    code = cli.main(["triage", "--service", "weather"])
    out = capsys.readouterr().out

    assert code == 0
    assert "=== Triage narrative ===" in out
    assert "the deploy did it" in out


def test_detect_receives_parsed_arguments(monkeypatch, capsys):
    """Flags must reach the pipeline unchanged."""
    seen = {}

    def fake_detect(**kwargs):
        seen.update(kwargs)
        return FakeBundle()

    monkeypatch.setattr("foundry.triage.pipeline.detect", fake_detect)

    cli.main(
        [
            "triage",
            "--service",
            "player-projections",
            "--endpoint",
            "/projections",
            "--incident",
            "error rate spike",
            "--prometheus-url",
            "http://prom:9090",
            "--gitops-dir",
            "custom/gitops",
            "--json",
        ]
    )

    assert seen == {
        "service": "player-projections",
        "endpoint": "/projections",
        "description": "error rate spike",
        "prometheus_url": "http://prom:9090",
        "gitops_dir": "custom/gitops",
    }
```

Note: `cli.main`'s trailing `return 1` is unreachable — `add_subparsers(required=True)`
with a single `triage` parser means argparse rejects anything else before that
line. Leave it; do not add a test that cannot execute, and do not delete it as
part of this phase.

- [ ] **Step 7: Run the foundry-cli suite**

```bash
cd services/foundry-cli && uv run pytest -q
```

Expected: PASS. `cli.py` above 0%; TOTAL at or above 88%.

- [ ] **Step 8: Lint and commit**

```bash
cd services/player-projections && uv run ruff check . && uv run ruff format --check .
cd ../foundry-cli && uv run ruff check . && uv run ruff format --check .
cd ../.. && git add contracts/openapi/player-projections.json services/player-projections/tests/ services/foundry-cli/tests/test_cli_entrypoint.py
git commit -m "test: OpenAPI snapshot, integration suites, CLI entrypoint coverage"
```

---

## Task 12: Helm collector endpoint render test

The cross-file check the design called for. The OTel collector DNS name exists only in the Helm chart; getting it wrong kills traces and logs silently while `/metrics` keeps working, because Prometheus scrapes pod annotations directly. `CLAUDE.md` documents this as a real past failure.

**Files:**
- Create: `tests/test_helm_otel_endpoint.py`

**Interfaces:**
- Consumes: `helm/charts/generic-service/values.yaml`, the `helm` binary
- Produces: nothing later tasks depend on

- [ ] **Step 1: Confirm the current rendered value**

```bash
helm template test helm/charts/generic-service | grep -i otel
```

Expected: the ConfigMap carries `http://otel-collector-opentelemetry-collector.monitoring.svc.cluster.local:4317`.

- [ ] **Step 2: Write the test**

Create `tests/test_helm_otel_endpoint.py`:

```python
"""Guards the OTel collector DNS name against the silent-failure mode
documented in CLAUDE.md: the Helmfile release is named `otel-collector`, but
the chart appends `-opentelemetry-collector`. A wrong name here stops traces
and logs while /metrics keeps working, because Prometheus scrapes pod
annotations directly and is unaffected.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "helm" / "charts" / "generic-service"
EXPECTED_ENDPOINT = (
    "http://otel-collector-opentelemetry-collector.monitoring.svc.cluster.local:4317"
)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not installed"
)


def _render() -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "test", str(CHART)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def test_values_declare_the_expected_collector_endpoint():
    values = yaml.safe_load((CHART / "values.yaml").read_text())

    assert values["otel"]["endpoint"] == EXPECTED_ENDPOINT


def test_rendered_configmap_carries_the_collector_endpoint():
    configmaps = [d for d in _render() if d.get("kind") == "ConfigMap"]

    assert configmaps, "chart rendered no ConfigMap"
    endpoints = [
        v
        for cm in configmaps
        for v in (cm.get("data") or {}).values()
        if isinstance(v, str) and "otel-collector" in v
    ]
    assert EXPECTED_ENDPOINT in endpoints


def test_endpoint_includes_the_chart_name_suffix():
    """The specific mistake: using the release name without the chart suffix."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    endpoint = values["otel"]["endpoint"]

    assert "otel-collector-opentelemetry-collector" in endpoint, (
        "endpoint is missing the `-opentelemetry-collector` suffix the Helm "
        "chart appends to the release name — traces and logs will silently stop"
    )
    assert endpoint.endswith(":4317"), "OTLP gRPC port must be 4317"
```

- [ ] **Step 3: Run it**

```bash
uv run --with pyyaml --with pytest pytest tests/test_helm_otel_endpoint.py -v
```

Expected: PASS, 3 tests. If `helm` is not installed locally the tests skip — the CI `helm-lint` job has it.

- [ ] **Step 4: Prove the test detects the failure mode**

```bash
sed -i.bak 's|otel-collector-opentelemetry-collector|otel-collector|' helm/charts/generic-service/values.yaml
uv run --with pyyaml --with pytest pytest tests/test_helm_otel_endpoint.py -q
```

Expected: FAIL on all three tests, with the suffix-specific message. Restore:

```bash
mv helm/charts/generic-service/values.yaml.bak helm/charts/generic-service/values.yaml
uv run --with pyyaml --with pytest pytest tests/test_helm_otel_endpoint.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_helm_otel_endpoint.py
git commit -m "test(helm): assert OTel collector DNS name against silent-failure mode"
```

---

## Task 13: Enable the coverage gate

Everything is above 80% by now. Switching the gate on is the last code change, so `main` was green at every prior commit.

**Files:**
- Modify: `services/weather/pyproject.toml`
- Modify: `services/player-projections/pyproject.toml`
- Modify: `services/foundry-cli/pyproject.toml`

**Interfaces:**
- Consumes: all prior tasks
- Produces: CI fails any PR dropping a package below 80%

- [ ] **Step 1: Record the pre-gate numbers**

```bash
for s in weather player-projections foundry-cli; do
  echo "== $s"; (cd services/$s && uv run pytest -q 2>&1 | grep TOTAL)
done
```

Expected: all three at or above 80%. **If any is below, stop and close the gap before continuing** — do not lower the threshold.

- [ ] **Step 2: Add the gate to all three services**

In each of the three `pyproject.toml` files, append `--cov-fail-under=80` to the existing `addopts`:

```toml
# services/weather/pyproject.toml
addopts = "--cov=weather --cov-branch --cov-report=term --cov-fail-under=80"
```

```toml
# services/player-projections/pyproject.toml
addopts = "--cov=player_projections --cov-branch --cov-report=term --cov-fail-under=80"
```

```toml
# services/foundry-cli/pyproject.toml
addopts = "--cov=foundry --cov-branch --cov-report=term --cov-fail-under=80"
```

- [ ] **Step 3: Verify the gate passes everywhere**

```bash
for s in weather player-projections foundry-cli; do
  echo "== $s"; (cd services/$s && uv run pytest -q) || echo "FAILED: $s"
done
```

Expected: all three PASS, no `FAILED` lines.

- [ ] **Step 4: Verify the gate actually blocks**

A gate that cannot fail is decoration. Prove it:

```bash
cd services/weather
sed -i.bak 's|--cov-fail-under=80|--cov-fail-under=99|' pyproject.toml
uv run pytest -q; echo "exit=$?"
mv pyproject.toml.bak pyproject.toml
uv run pytest -q; echo "exit=$?"
```

Expected: first run exits non-zero with `Coverage failure: total of ... is less than fail-under=99`; second exits 0.

- [ ] **Step 5: Commit**

```bash
git add services/weather/pyproject.toml services/player-projections/pyproject.toml services/foundry-cli/pyproject.toml
git commit -m "test: enforce 80% branch coverage gate across all packages"
```

---

## Task 14: Documentation

The ADR, the strategy doc, and the three corrections identified during design.

**Files:**
- Create: `docs/adr/0002-provider-driven-contracts.md`
- Create: `docs/testing-strategy.md`
- Modify: `docs/architecture/phase-5-resilience-and-ai-testing.md`
- Modify: `CLAUDE.md`
- Modify: `services/weather/README.md`

**Interfaces:**
- Consumes: all prior tasks
- Produces: nothing

- [ ] **Step 1: Check the existing ADR format**

```bash
cat docs/adr/0001-eks-cost-and-minimum-sizing.md | head -30
```

Match its heading structure and status-line convention in the new ADR.

- [ ] **Step 2: Write ADR 0002**

Create `docs/adr/0002-provider-driven-contracts.md`, following the format from Step 1:

```markdown
# ADR 0002 — Provider-Driven Contract Testing

**Status:** Accepted
**Date:** 2026-07-27
**Context:** Phase 5A — Rigorous Service & Platform Testing

## Context

Phase 5A introduces contract testing. Two hops need contracts:

| Hop | Shape | State |
|---|---|---|
| `player-data` → `player-projections` | S3 JSON document, polled | Provider not built |
| `player-projections` → `fantasy-frontend` | HTTP request/response | Consumer not built |

The reference spec named Pact. Pact implements *consumer-driven* contract
testing: each consumer declares what it needs, and those expectations are
replayed in the provider's CI.

## Decision

Enforce contracts **provider-side**, with committed schemas:

- **JSON Schema** for the `player-data` snapshot documents, one per scoring
  format, in `contracts/player-data/`.
- **Committed OpenAPI snapshots** for `weather` and `player-projections`, in
  `contracts/openapi/`, with CI failing on undeclared divergence.

Do not adopt Pact.

## Rationale

**Foundry is a monorepo and owns both sides of every hop.** Pact's purpose is
coordinating independently deployed services across separate repos and CI
systems. When consumer and provider land in the same CI run, the real consumer
can be tested against the real provider — strictly better than a pact, which is
a recorded approximation of that interaction.

**Neither hop currently has both sides.** `player-data` does not exist;
`fantasy-frontend` does not exist. Pact today would be half-inert either way.

**The upstream hop is a document, not request/response.** Pact covers this via
message pacts, but that is its least-exercised path in Python and it buys less
than a schema. JSON Schema is the native format for a document contract: any
future `player-data`, in any language, validates against the same file with a
stock library.

## What This Gives Up

Provider-driven schemas are not consumer-driven. They do not record *which
fields the consumer actually reads*, so they cannot tell a future `player-data`
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
```

- [ ] **Step 3: Write the testing strategy doc**

Create `docs/testing-strategy.md`:

```markdown
# Testing Strategy

What is tested at each layer, why, and where it runs.

## Layers

| Layer | Scope | Mocks | Runs in |
|---|---|---|---|
| Unit | One function or module | `respx` stubs the HTTP boundary | per-service `test` job |
| Property | Parser robustness under generated input | `respx` | per-service `test` job |
| Contract | Payload shape, API surface stability | none — schemas and snapshots | per-service `test` job |
| Integration | Whole app over real HTTP | only the external upstream | per-service `test` job |
| Helm render | Chart output correctness | none — real `helm template` | root `tests/` |
| Smoke | Deployed services in a Kind cluster | none | `integration-test` job |

## Coverage

80% line **and branch**, enforced by `--cov-fail-under=80` in each package's
`pyproject.toml`. **No files are excluded from measurement.** Excluding a file
to reach a number is hiding the gap the gate exists to surface — `telemetry.py`
sat at 0% in every service until Phase 5A and is now covered by tests that
assert wiring rather than SDK internals.

Coverage is reported to the GitHub Actions job summary on every run, including
failed ones.

## What Each Layer Catches

**Unit** — logic errors in a single unit, with collaborators stubbed.

**Property (Hypothesis)** — the failure class hand-written tests miss: missing
fields, wrong types, non-object bodies, oversized payloads. These found two real
defects in `player-projections` during Phase 5A: an untyped `AttributeError` on
a non-object snapshot, and a single malformed record discarding an entire batch.

**Contract** — that the shape other systems depend on has not changed silently.
The OpenAPI snapshot test is the automated catch for "response field renamed",
a fault type in the Phase 5C catalog. Intentional API changes regenerate the
snapshot in the same PR, making every surface change explicit in review.

**Integration** — that the wired-together app behaves correctly over HTTP:
concurrency, upstream timeouts, malformed upstream responses, per-item
degradation.

**Helm render** — cross-file consistency that no service test can see. The OTel
collector DNS name is the standing example: wrong value, traces and logs stop
silently while `/metrics` keeps working.

**Smoke** — that the deployed thing actually serves traffic in a cluster.

## Adding Tests for a New Service

1. Copy the `pyproject.toml` coverage block from `services/weather`.
2. Write unit tests with `respx` for the upstream boundary.
3. Add a property suite for any parser handling external data.
4. Commit an OpenAPI snapshot to `contracts/openapi/<service>.json` and add the
   divergence test.
5. Add an integration suite under `tests/integration/`.
6. Confirm the 80% gate passes before opening the PR.

## Not Covered Here

Chaos scenarios, load and scale testing, and adversarial agent sessions are
Phase 5B and 5C. See `docs/architecture/phase-5-resilience-and-ai-testing.md`.
```

- [ ] **Step 4: Reconcile the Phase 5 architecture doc**

In `docs/architecture/phase-5-resilience-and-ai-testing.md`, update the **Stage 1 Deliverables** list so it matches what was built. Replace the Pact and coverage-report entries:

```markdown
### Deliverables

- Updated `pyproject.toml` per service with coverage thresholds and branch coverage
- `contracts/player-data/` — JSON Schema contracts for the three scoring-format snapshots
- `contracts/openapi/` — committed OpenAPI snapshots with CI divergence detection
- `services/*/tests/test_properties.py` — Hypothesis suites for all external data parsers
- `services/*/tests/integration/` — real HTTP integration tests per service
- `tests/test_helm_otel_endpoint.py` — Helm render assertion for the collector DNS name
- `.github/workflows/foundry-cli.yml` — CI for the triage engine (previously untested)
- Coverage reported to the GitHub Actions job summary — **supersedes** the
  originally specified `.github/actions/coverage-report/` composite action;
  enforcement is handled by `--cov-fail-under`, so the action would have added a
  workflow permission and base-branch bookkeeping for reporting alone
- `docs/testing-strategy.md` — what is tested at each layer and why
- `docs/adr/0002-provider-driven-contracts.md` — why schema-first, not Pact
```

Then update the Stage 1 line in **Deliverables Summary** and the first **Milestones** checkbox to match, replacing "Pact contract tests" with "schema contract tests".

- [ ] **Step 5: Resolve the CLAUDE.md contradiction**

`CLAUDE.md` currently describes `player-data` as both API-key-gated and S3-with-no-key. In the **Long-Term Vision** section, change:

```
  └── never exposed publicly; accessed only by internal services via API key
```

to:

```
  └── never exposed publicly; publishes curated snapshots to S3 that internal
      services poll (S3 auth handled at the infrastructure level — see ADR 0002)
```

In the **player-projections — How It Works** section, update the upstream description to reflect three per-format files rather than one:

```
**Upstream architecture:** `player-data` aggregates data from internal sources —
weather, injury reports, betting lines, news, field type — and writes one
curated projections JSON document per scoring format (standard, half-PPR, PPR)
to S3. `player-projections` polls the document matching the requested format on
an interval and caches the result in memory. The document shape is contracted in
`contracts/player-data/` — see `docs/testing-strategy.md`.
```

- [ ] **Step 6: Remove the trademark references**

In `services/weather/README.md`, replace both occurrences:

- Line 3: `Current conditions by NFL stadium location.` → `Current conditions by pro football stadium location.`
- Line 11: `Current conditions for all 30 NFL stadiums` → `Current conditions for all 30 pro football stadiums`

Confirm none remain anywhere:

```bash
grep -rn "NFL" --exclude-dir=.git --exclude="*.lock" . || echo "clean"
```

Expected: `clean`.

- [ ] **Step 7: Commit**

```bash
git add docs/ CLAUDE.md services/weather/README.md
git commit -m "docs: add ADR 0002 and testing strategy, reconcile phase-5 deliverables"
```

---

## Final Verification

- [ ] **Step 1: Full suite, all packages**

```bash
for s in weather player-projections foundry-cli; do
  echo "===== $s"; (cd services/$s && uv run pytest -q) || echo "FAILED: $s"
done
uv run --with pyyaml --with pytest pytest tests/ -q
```

Expected: all pass, no `FAILED` lines. Every package reports TOTAL at or above 80%.

- [ ] **Step 2: Lint**

```bash
for s in weather player-projections foundry-cli; do
  (cd services/$s && uv run ruff check . && uv run ruff format --check .) || echo "LINT FAILED: $s"
done
```

Expected: no `LINT FAILED` lines.

- [ ] **Step 3: Helm lint**

```bash
helm lint helm/charts/generic-service
```

Expected: `1 chart(s) linted, 0 chart(s) failed`.

- [ ] **Step 4: Confirm no workflow permissions were added**

```bash
grep -rn "permissions:" .github/workflows/ || echo "none added"
```

Expected: only pre-existing entries — Phase 5A adds none.

- [ ] **Step 5: Run pr-uat**

Required by `CLAUDE.md` before any final PR. Invoke the `superpowers:pr-uat` skill and complete every step: unit tests, service startup, HTTP endpoints, Docker build, container runtime, Helm render, Helm lint, CI action reference resolution.

- [ ] **Step 6: Open the PR**

Title: `Phase 5A: Rigorous Service & Platform Testing`

The body should note the two defects found and fixed (`foundry-cli` packaging and missing CI workflow; `player-projections` malformed-record handling), and state explicitly that **Phase 5 is not complete** — 5B (chaos + scale) and 5C (adversarial agents) remain, so the `phase-5` tag and README status flip are deferred.

---

## Out of Scope — Do Not Build

Listed so an executing agent does not helpfully add them:

- **Chaos Mesh, chaos scenarios, k6 load tests, scale baselines** — Phase 5B.
- **AI adversarial agents, fault catalog, `scripts/run-adversarial.py`** — Phase 5C.
- **Pact, a Pact broker, or `pact-python`** — see ADR 0002.
- **Format selection in `player-projections`** (`?scoring=ppr` routing to a
  per-format S3 file). The schemas describe the shape; wiring the consumer to
  choose among three upstream files belongs with the `player-data` build, since
  there is nothing to select from until it exists.
- **A coverage-report composite action or PR-comment bot** — superseded by the
  job summary in Task 1.
- **Coverage gating for the repo-root `tests/`** (`test_rollback.py`,
  `test_argocd_deploy.py`). Those cover `scripts/`, which is out of scope here.
- **Raising the gate above 80%.** If a package lands higher, leave the
  threshold at 80 — a ratcheting threshold makes unrelated PRs fail.

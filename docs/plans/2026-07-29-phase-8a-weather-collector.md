# Phase 8A PR #1 — `weather` as the First Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the collector contract — signal envelope, shared capture library, and append-only signal lake — by retrofitting `weather` from a stateless proxy into a collector that captures on a cadence and serves the latest snapshot from memory.

**Architecture:** A new `libs/collector-core/` uv workspace member holds everything reusable across the eventual 26-collector fleet: envelope construction, coverage accounting, cadence scheduling, lake writes, and the force-refresh interval floor. `services/weather/` consumes it by path dependency and supplies only what is genuinely per-source — two adapters (schedule, forecast), a venue table, and an environment resolver. The library is extracted from a working consumer rather than designed in the abstract, which is why `weather` goes first despite the phase doc listing `player-identity` first.

**Tech Stack:** Python 3.12, FastAPI, uv workspaces, httpx, respx, pytest, pytest-asyncio, boto3, MinIO, Helm 3, Helmfile, Kind.

**Reference design:** `docs/superpowers/specs/2026-07-29-phase-8a-weather-collector-design.md` (local, gitignored)
**Reference spec:** `docs/architecture/phase-8-data-source-collectors.md`

---

## Global Constraints

- **Python** `>=3.12` everywhere. Line length 88, ruff lint `select = ["E", "F", "I"]`.
- **Coverage gate is 80%** line + branch, no omitted files. Applies to `libs/collector-core/` as well as `services/weather/`.
- **Do not use the term "NFL" anywhere** in code, comments, docs, or test names. The project does not hold those rights. Use "pro football", "the league", or "stadium". **Exemption (ruled 2026-07-29):** third-party proper nouns we do not control — the `nflverse`/`nfldata` URL in the schedule adapter, package names, upstream field names — are exempt, because they are identifiers rather than prose. Do not rewrite the schedule URL; it will 404.
- **Every `pyproject.toml` dependency change must be followed by `uv lock`**, with the regenerated lock committed in the same commit. Verify with `uv lock --check` (exit 0 = current, exit 1 = stale).

  **Discovered during Task 1:** a uv workspace has **one lockfile at the workspace root**, not one per member. `services/weather/uv.lock` was deleted and `uv.lock` at the repo root replaces it. Running `uv lock`, `uv lock --check`, or `uv sync --frozen` from inside a member directory resolves to the root lock — verified working for the `python-test` composite action and for `platform-tests`. `services/player-projections/uv.lock` stays where it is; that service is not a workspace member.
- **No new GitHub Actions workflow or composite-action FILES.** Existing `weather.yml` and the existing composite actions are extended in place (Task 16 steps 9-10): a `collector-core` lint+test job inside `weather.yml`, `libs/**` added to its path filters, and an optional `context` input on `.github/actions/build-push`. Adding a new `.yml` under `.github/workflows/` or `.github/actions/` is out of scope.
- **`/health` and `/metrics` stay auth-exempt.** The kubelet's probes and Prometheus's annotation scrape cannot carry a token.
- **The lake is append-only.** Never mutate or delete an object in place. A correction is a new object with a later `captured_at`.
- **A collector never proxies.** `/signals` reads the in-memory cache only. An upstream outage degrades freshness, never availability.

### Verified upstream facts (confirmed 2026-07-29 against live data — do not re-derive)

Source: `https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv`, 7,548 rows, 272 for season 2026.

| Fact | Value |
|---|---|
| `game_id` format | `2026_01_NE_SEA` — season, 2-digit week, away, home |
| `gametime` | 24-hour **Eastern**, always — never local to the venue, never UTC |
| `roof` values | `outdoors` (177) · `dome` (52) · **empty (43)** |
| Empty `roof` | Exactly the five retractable venues: `ATL97`, `DAL00`, `HOU00`, `IND00`, `PHO00` |
| `temp` / `wind` | **0 of 272 populated** for future games — post-game only, unusable as forecast |
| `location` | `Home` (264) · `Neutral` (8) |
| **Neutral-site trap** | For `location == 'Neutral'`, `stadium_id` and `roof` describe the **designated home team's** stadium, not the actual venue. Only `stadium` (name) is correct. |

Roof mapping:

```
outdoors -> outdoor              dome   -> fixed_dome
open     -> retractable_open     closed -> retractable_closed
(empty)  -> retractable_undecided   [only valid at a known-retractable venue]
```

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `libs/collector-core/pyproject.toml` | Workspace member metadata |
| `libs/collector-core/collector_core/envelope.py` | `Envelope`, `Upstream`, `Coverage` dataclasses and serialization |
| `libs/collector-core/collector_core/coverage.py` | `CoverageAccumulator` — declare expected, record present, derive missing |
| `libs/collector-core/collector_core/cadence.py` | `CadenceClass` enum and interval resolution incl. perishable escalation |
| `libs/collector-core/collector_core/lake.py` | `LakeWriter` protocol, `S3LakeWriter`, key layout |
| `libs/collector-core/collector_core/refresh.py` | `RefreshGate` — minimum-interval floor, 429 signal |
| `libs/collector-core/tests/` | Library unit tests |
| `contracts/signal-envelope/envelope.v1.schema.json` | The envelope contract |
| `contracts/signal-envelope/collectors/weather.json` | Weather's field-level shape |
| `services/weather/weather/adapters/__init__.py` | Adapter package marker |
| `services/weather/weather/adapters/schedule.py` | Schedule feed -> `ScheduledGame`, incl. neutral-site gate and ET->UTC |
| `services/weather/weather/adapters/forecast.py` | Open-Meteo -> normalized imperial forecast fields with bands |
| `services/weather/weather/environment.py` | Roof + venue -> `environment` enum |
| `services/weather/weather/playability.py` | Derived `kicking_difficulty`, `deep_pass_penalty`, `ball_security_risk` |
| `services/weather/weather/capture.py` | Orchestration: schedule -> environment -> forecast -> envelope -> lake + cache |
| `libs/collector-core/collector_core/auth.py` | Shared bearer-token middleware |
| `libs/collector-core/collector_core/metrics.py` | Shared fleet metrics, parameterized by collector |
| `libs/collector-core/collector_core/routes.py` | Mountable router for the five standard routes |
| `libs/collector-core/collector_core/scheduler.py` | The capture loop and the perishable escalation |
| `infra/grafana-stack/values/minio.yaml` | MinIO release values for the local stack |
| `tests/test_signal_envelope_conformance.py` | Platform-level envelope conformance |

**Modified:**

| Path | Change |
|---|---|
| `services/weather/weather/stadiums.py` | Add `stadium_id` and `roof_type` per venue |
| `services/weather/weather/main.py` | Replace stadium routes with the five contract routes + convergence |
| `services/weather/weather/client.py` | Absorbed into `adapters/forecast.py`; file deleted |
| `services/weather/pyproject.toml` | Add `collector-core` path dep, `boto3`; uv workspace member |
| `services/weather/Dockerfile` | Copy `libs/collector-core/` into the build stage |
| `services/weather/tests/*` | Rewritten against the new surface |
| `contracts/openapi/weather.json` | Regenerated |
| `contracts/responses/weather.json` | Regenerated |
| `infra/grafana-stack/helmfile.yaml` | Add the MinIO release |
| `helm/values/weather/values.yaml` | Lake + cadence env vars |
| `scripts/deploy-local.py` | MinIO credentials Secret |
| `scripts/smoke-test.sh` | Assertions rewritten against `/signals` |
| `.github/workflows/weather.yml` | `libs/**` path filter, collector-core lint+test jobs, repo-root build context |
| `.github/actions/build-push/action.yml` | Optional `context` and `dockerfile` inputs, defaulting to today's behaviour |
| `helm/charts/generic-service/templates/httproute.yaml` | Publish only the collector's declared contract paths, not the auth-exempt ones |
| `docs/architecture/phase-8-data-source-collectors.md` | Amend coverage window + 8A dependencies |
| `CLAUDE.md`, `docs/onboarding.md` | Workspace-member Dockerfile pattern |
| `pyproject.toml` (repo root) | Declare the uv workspace |

---

## Task 1: uv workspace + `collector-core` skeleton

**Files:**
- Create: `pyproject.toml` (repo root), `libs/collector-core/pyproject.toml`, `libs/collector-core/collector_core/__init__.py`, `libs/collector-core/tests/test_package.py`
- Modify: `services/weather/pyproject.toml`

**Interfaces:**
- Consumes: nothing
- Produces: importable `collector_core` package for every later task

- [ ] **Step 1: Write the failing test**

`libs/collector-core/tests/test_package.py`:

```python
def test_package_exposes_version():
    import collector_core

    assert collector_core.__version__ == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_package.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core'`

- [ ] **Step 3: Create the root workspace declaration**

`pyproject.toml` at repo root:

```toml
[tool.uv.workspace]
members = ["libs/*", "services/weather"]
```

- [ ] **Step 4: Create the library package**

`libs/collector-core/pyproject.toml`:

```toml
[project]
name = "collector-core"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["boto3"]

[dependency-groups]
dev = ["ruff", "pytest", "pytest-asyncio", "pytest-cov", "jsonschema", "moto[s3]"]

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
addopts = "--cov=collector_core --cov-branch --cov-report=term --cov-fail-under=80"

[tool.coverage.run]
branch = true
source = ["collector_core"]

[tool.coverage.report]
show_missing = true
```

`libs/collector-core/collector_core/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 5: Add the path dependency to weather**

In `services/weather/pyproject.toml`, add to `[project].dependencies`:

```toml
    "collector-core",
```

and append:

```toml
[tool.uv.sources]
collector-core = { workspace = true }
```

- [ ] **Step 6: Regenerate locks**

```bash
cd libs/collector-core && uv lock && uv lock --check
cd ../../services/weather && uv lock && uv lock --check
```

Expected: both exit 0.

- [ ] **Step 7: Run test to verify it passes**

Run: `cd libs/collector-core && uv run pytest tests/test_package.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml libs/ services/weather/pyproject.toml services/weather/uv.lock
git commit -m "feat(collector-core): scaffold shared capture library as uv workspace member"
```

---

## Task 2: The signal envelope contract and model

**Files:**
- Create: `contracts/signal-envelope/envelope.v1.schema.json`, `libs/collector-core/collector_core/envelope.py`, `libs/collector-core/tests/test_envelope.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Upstream(adapter: str, fetched_at: datetime, source_ref: str | None = None)`
  - `Coverage(expected: int, present: int, missing: list[str])`
  - `Envelope(envelope_version: str, collector: str, signal_type: str, captured_at: datetime, upstream: Upstream, scope: dict, coverage: Coverage, errors: list[dict], signals: list[dict])`
  - `Envelope.to_dict() -> dict` — RFC 3339 UTC timestamps with a `Z` suffix
  - `ENVELOPE_VERSION: str = "1"`

- [ ] **Step 1: Write the failing test**

`libs/collector-core/tests/test_envelope.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream

SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "signal-envelope"
    / "envelope.v1.schema.json"
)


def test_to_dict_serializes_timestamps_as_rfc3339_utc():
    body = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=datetime(2026, 9, 17, 14, 3, tzinfo=UTC),
        upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, 57, tzinfo=UTC)),
        scope={"season": 2026, "week": 3},
        coverage=Coverage(expected=1, present=1, missing=[]),
        errors=[],
        signals=[{"game_id": "2026_03_KC_BUF"}],
    ).to_dict()

    assert body["captured_at"] == "2026-09-17T14:03:00Z"
    assert body["upstream"]["fetched_at"] == "2026-09-17T14:02:57Z"


def test_to_dict_validates_against_the_committed_schema():
    body = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=datetime(2026, 9, 17, 14, 3, tzinfo=UTC),
        upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, 57, tzinfo=UTC)),
        scope={"season": 2026, "week": 3},
        coverage=Coverage(expected=16, present=15, missing=["2026_03_BAL_DAL"]),
        errors=[],
        signals=[{"game_id": "2026_03_KC_BUF"}],
    ).to_dict()

    jsonschema.validate(body, json.loads(SCHEMA.read_text()))


def test_naive_captured_at_is_rejected():
    """A naive datetime silently means 'some timezone' and lands wrong in the lake."""
    with pytest.raises(ValueError, match="timezone-aware"):
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="weather",
            signal_type="venue_forecast_kickoff",
            captured_at=datetime(2026, 9, 17, 14, 3),
            upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, tzinfo=UTC)),
            scope={"season": 2026, "week": 3},
            coverage=Coverage(expected=1, present=1, missing=[]),
            errors=[],
            signals=[],
        )


def test_failed_capture_envelope_is_valid():
    """A total failure still writes — that is how a gap becomes explicit."""
    body = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=datetime(2026, 9, 17, 14, 3, tzinfo=UTC),
        upstream=Upstream("open-meteo", datetime(2026, 9, 17, 14, 2, tzinfo=UTC)),
        scope={"season": 2026, "week": 3},
        coverage=Coverage(expected=16, present=0, missing=[f"g{i}" for i in range(16)]),
        errors=[{"reason": "timeout", "detail": "upstream did not respond"}],
        signals=[],
    ).to_dict()

    jsonschema.validate(body, json.loads(SCHEMA.read_text()))
    assert body["coverage"]["present"] == 0
    assert body["errors"][0]["reason"] == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_envelope.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.envelope'`

- [ ] **Step 3: Write the contract**

`contracts/signal-envelope/envelope.v1.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://foundry.internal/signal-envelope/envelope.v1.schema.json",
  "title": "Foundry collector signal envelope, version 1",
  "type": "object",
  "required": [
    "envelope_version",
    "collector",
    "signal_type",
    "captured_at",
    "upstream",
    "scope",
    "coverage",
    "errors",
    "signals"
  ],
  "additionalProperties": false,
  "properties": {
    "envelope_version": { "const": "1" },
    "collector": { "type": "string", "minLength": 1 },
    "signal_type": { "type": "string", "minLength": 1 },
    "captured_at": { "type": "string", "format": "date-time" },
    "upstream": {
      "type": "object",
      "required": ["adapter", "fetched_at"],
      "additionalProperties": false,
      "properties": {
        "adapter": { "type": "string", "minLength": 1 },
        "fetched_at": { "type": "string", "format": "date-time" },
        "source_ref": { "type": ["string", "null"] }
      }
    },
    "scope": { "type": "object" },
    "coverage": {
      "type": "object",
      "required": ["expected", "present", "missing"],
      "additionalProperties": false,
      "properties": {
        "expected": { "type": "integer", "minimum": 0 },
        "present": { "type": "integer", "minimum": 0 },
        "missing": { "type": "array", "items": { "type": "string" } }
      }
    },
    "errors": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["reason"],
        "properties": {
          "reason": { "type": "string" },
          "detail": { "type": "string" }
        }
      }
    },
    "signals": { "type": "array", "items": { "type": "object" } }
  }
}
```

- [ ] **Step 4: Write the model**

`libs/collector-core/collector_core/envelope.py`:

```python
"""The envelope every collector emits, in HTTP responses and lake objects alike.

Contracted in contracts/signal-envelope/. The `coverage` block is the part worth
defending: without it a collector returning 309 of 312 rows is indistinguishable
from a healthy one, and the generator quietly trains on a hole.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

ENVELOPE_VERSION = "1"


def _rfc3339(value: datetime) -> str:
    """Serialize as RFC 3339 UTC with a `Z` suffix.

    Rejects naive datetimes rather than assuming UTC. A naive timestamp means
    'some timezone the caller forgot to state', and guessing puts a wrong
    instant into an append-only lake that is never rewritten.
    """
    if value.tzinfo is None:
        raise ValueError(f"timezone-aware datetime required, got naive: {value!r}")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Upstream:
    adapter: str
    fetched_at: datetime
    source_ref: str | None = None

    def to_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "fetched_at": _rfc3339(self.fetched_at),
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class Coverage:
    expected: int
    present: int
    missing: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """present/expected, or 1.0 when nothing was expected.

        An empty week is complete, not broken — a bye week legitimately expects
        zero records, and 0/0 must not read as a coverage failure.
        """
        return 1.0 if self.expected == 0 else self.present / self.expected

    def to_dict(self) -> dict:
        return {
            "expected": self.expected,
            "present": self.present,
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class Envelope:
    envelope_version: str
    collector: str
    signal_type: str
    captured_at: datetime
    upstream: Upstream
    scope: dict
    coverage: Coverage
    errors: list[dict]
    signals: list[dict]

    def __post_init__(self) -> None:
        # Validate eagerly rather than at serialization time, so a bad timestamp
        # fails where it was constructed instead of deep in a lake write.
        _rfc3339(self.captured_at)
        _rfc3339(self.upstream.fetched_at)

    def to_dict(self) -> dict:
        return {
            "envelope_version": self.envelope_version,
            "collector": self.collector,
            "signal_type": self.signal_type,
            "captured_at": _rfc3339(self.captured_at),
            "upstream": self.upstream.to_dict(),
            "scope": dict(self.scope),
            "coverage": self.coverage.to_dict(),
            "errors": list(self.errors),
            "signals": list(self.signals),
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_envelope.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add contracts/signal-envelope/ libs/collector-core/
git commit -m "feat(collector-core): signal envelope contract and model"
```

---

## Task 3: Coverage accounting

**Files:**
- Create: `libs/collector-core/collector_core/coverage.py`, `libs/collector-core/tests/test_coverage.py`

**Interfaces:**
- Consumes: `Coverage` from Task 2
- Produces: `CoverageAccumulator(expected_keys: Iterable[str])` with `.record(key: str) -> None`, `.fail(key: str, reason: str) -> None`, `.result() -> Coverage`, `.errors -> list[dict]`

- [ ] **Step 1: Write the failing test**

`libs/collector-core/tests/test_coverage.py`:

```python
import pytest

from collector_core.coverage import CoverageAccumulator


def test_all_present_gives_full_coverage():
    acc = CoverageAccumulator(["a", "b", "c"])
    for key in ("a", "b", "c"):
        acc.record(key)

    result = acc.result()
    assert result.expected == 3
    assert result.present == 3
    assert result.missing == []
    assert result.ratio == 1.0


def test_missing_is_derived_not_declared():
    """Missing is expected-minus-present, so it cannot drift out of sync."""
    acc = CoverageAccumulator(["a", "b", "c"])
    acc.record("a")

    result = acc.result()
    assert result.present == 1
    assert result.missing == ["b", "c"]


def test_missing_is_sorted_for_stable_diffs():
    acc = CoverageAccumulator(["c", "a", "b"])
    result = acc.result()
    assert result.missing == ["a", "b", "c"]


def test_failure_records_reason_and_leaves_key_missing():
    acc = CoverageAccumulator(["a", "b"])
    acc.record("a")
    acc.fail("b", "no_venue_coordinates")

    result = acc.result()
    assert result.present == 1
    assert result.missing == ["b"]
    assert acc.errors == [{"reason": "no_venue_coordinates", "detail": "b"}]


def test_total_failure_still_produces_a_result():
    """A poll that fails entirely still writes — present 0, everything missing."""
    acc = CoverageAccumulator(["a", "b"])
    acc.fail("a", "timeout")
    acc.fail("b", "timeout")

    result = acc.result()
    assert result.expected == 2
    assert result.present == 0
    assert result.missing == ["a", "b"]
    assert result.ratio == 0.0


def test_empty_expectation_is_complete_not_broken():
    """A bye week expects nothing. 0/0 must read as 1.0, not as a failure."""
    result = CoverageAccumulator([]).result()
    assert result.expected == 0
    assert result.ratio == 1.0


def test_recording_an_unexpected_key_raises():
    """Recording a key nobody expected means the expectation set is wrong."""
    acc = CoverageAccumulator(["a"])
    with pytest.raises(KeyError, match="not in the expected set"):
        acc.record("z")


def test_recording_twice_is_idempotent():
    acc = CoverageAccumulator(["a"])
    acc.record("a")
    acc.record("a")
    assert acc.result().present == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_coverage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.coverage'`

- [ ] **Step 3: Write the implementation**

`libs/collector-core/collector_core/coverage.py`:

```python
"""Coverage accounting — 'what does complete mean here?' made mechanical.

Expected keys are declared up front and present ones recorded as they land, so
`missing` is derived rather than maintained. A collector cannot report itself
complete while silently dropping rows, because the two numbers come from one
source.
"""

from collections.abc import Iterable

from .envelope import Coverage


class CoverageAccumulator:
    def __init__(self, expected_keys: Iterable[str]) -> None:
        self._expected: set[str] = set(expected_keys)
        self._present: set[str] = set()
        self._errors: list[dict] = []

    def record(self, key: str) -> None:
        """Mark a key captured. Idempotent."""
        if key not in self._expected:
            raise KeyError(f"{key!r} is not in the expected set")
        self._present.add(key)

    def fail(self, key: str, reason: str) -> None:
        """Record why a key could not be captured. It stays missing."""
        self._errors.append({"reason": reason, "detail": key})

    @property
    def errors(self) -> list[dict]:
        return list(self._errors)

    def result(self) -> Coverage:
        return Coverage(
            expected=len(self._expected),
            present=len(self._present),
            missing=sorted(self._expected - self._present),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_coverage.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add libs/collector-core/
git commit -m "feat(collector-core): coverage accounting with derived missing set"
```

---

## Task 4: The signal lake writer

**Files:**
- Create: `libs/collector-core/collector_core/lake.py`, `libs/collector-core/tests/test_lake.py`

**Interfaces:**
- Consumes: `Envelope` from Task 2
- Produces:
  - `lake_key(envelope: Envelope) -> str`
  - `LakeWriter` protocol with `write(envelope) -> str`
  - `S3LakeWriter(bucket: str, client)` with `.write(envelope) -> str`, `.list_keys(collector, signal_type, season, week) -> list[str]`, `.read(key) -> dict`
  - `NullLakeWriter()` — same interface, discards. Used in tests and when `LAKE_BUCKET` is unset.
  - `build_lake_writer_from_env() -> LakeWriter`

- [ ] **Step 1: Write the failing test**

`libs/collector-core/tests/test_lake.py`:

```python
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import NullLakeWriter, S3LakeWriter, lake_key

BUCKET = "foundry-signals-test"


def envelope(captured_at: datetime, week: int = 3, signals=None) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=captured_at,
        upstream=Upstream("open-meteo", captured_at),
        scope={"season": 2026, "week": week},
        coverage=Coverage(expected=1, present=1, missing=[]),
        errors=[],
        signals=signals if signals is not None else [{"game_id": "2026_03_KC_BUF"}],
    )


def test_key_layout_partitions_by_season_and_week():
    key = lake_key(envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC)))
    assert key == (
        "signals/weather/v1/season=2026/week=03/2026-09-17T14:03:00Z.json"
    )


def test_week_is_zero_padded_so_prefix_scans_sort():
    key = lake_key(envelope(datetime(2026, 9, 10, 9, 0, tzinfo=UTC), week=4))
    assert "week=04/" in key


@mock_aws
def test_write_puts_an_object_and_returns_its_key():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    key = writer.write(envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC)))

    body = writer.read(key)
    assert body["collector"] == "weather"
    assert body["signals"][0]["game_id"] == "2026_03_KC_BUF"


@mock_aws
def test_two_captures_of_the_same_scope_are_two_objects():
    """Append-only: a later capture never overwrites an earlier one."""
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    writer.write(envelope(datetime(2026, 9, 15, 9, 0, tzinfo=UTC)))
    writer.write(envelope(datetime(2026, 9, 17, 9, 0, tzinfo=UTC)))

    keys = writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3)
    assert len(keys) == 2


@mock_aws
def test_list_keys_returns_captured_at_order():
    """The convergence route depends on this ordering."""
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    for day in (17, 15, 16):
        writer.write(envelope(datetime(2026, 9, day, 9, 0, tzinfo=UTC)))

    keys = writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3)
    assert keys == sorted(keys)
    assert "2026-09-15" in keys[0]
    assert "2026-09-17" in keys[-1]


@mock_aws
def test_list_keys_on_an_empty_partition_returns_empty():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    assert writer.list_keys("weather", "venue_forecast_kickoff", 2026, 9) == []


def test_null_writer_satisfies_the_interface_and_discards():
    writer = NullLakeWriter()
    key = writer.write(envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC)))
    assert key == ""
    assert writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3) == []


def test_null_writer_read_raises():
    with pytest.raises(KeyError):
        NullLakeWriter().read("anything")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_lake.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.lake'`

- [ ] **Step 3: Write the implementation**

`libs/collector-core/collector_core/lake.py`:

```python
"""Append-only signal lake.

Objects are never mutated or deleted in place. A correction lands as a new
object with a later `captured_at`, and the generator resolves by recency —
nothing is lost, and the revision itself is visible.

Partitioning by season and week means a training window is one prefix scan
rather than a full-bucket listing.
"""

import json
import os
from typing import Protocol

import boto3

from .envelope import Envelope


def lake_key(envelope: Envelope) -> str:
    """signals/<collector>/v<version>/season=<YYYY>/week=<NN>/<captured_at>.json

    Week is zero-padded so lexicographic prefix listing is also chronological
    ordering — week=10 must not sort before week=2.
    """
    captured_at = envelope.to_dict()["captured_at"]
    season = envelope.scope["season"]
    week = int(envelope.scope["week"])
    return (
        f"signals/{envelope.collector}/v{envelope.envelope_version}"
        f"/season={season}/week={week:02d}/{captured_at}.json"
    )


def _partition_prefix(collector: str, season: int, week: int) -> str:
    return f"signals/{collector}/v1/season={season}/week={week:02d}/"


class LakeWriter(Protocol):
    def write(self, envelope: Envelope) -> str: ...

    def list_keys(
        self, collector: str, signal_type: str, season: int, week: int
    ) -> list[str]: ...

    def read(self, key: str) -> dict: ...


class S3LakeWriter:
    """S3-API backend. Real S3 on EKS, MinIO on Kind — same code path."""

    def __init__(self, bucket: str, client) -> None:
        self._bucket = bucket
        self._client = client

    def write(self, envelope: Envelope) -> str:
        key = lake_key(envelope)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(envelope.to_dict(), sort_keys=True).encode(),
            ContentType="application/json",
        )
        return key

    def list_keys(
        self, collector: str, signal_type: str, season: int, week: int
    ) -> list[str]:
        """Keys in the partition, in captured_at order.

        `signal_type` is not part of the key layout — one capture writes one
        envelope per signal type into the same partition — so results are
        filtered by reading, not by prefix. Sorted because the convergence
        route depends on the ordering.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=_partition_prefix(collector, season, week)
        ):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)

    def read(self, key: str) -> dict:
        body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return json.loads(body)


class NullLakeWriter:
    """Discards writes. Used in tests and whenever LAKE_BUCKET is unset.

    Deliberately not a silent no-op in production: build_lake_writer_from_env
    logs at construction, so a collector running without a lake is visible
    rather than assumed.
    """

    def write(self, envelope: Envelope) -> str:
        return ""

    def list_keys(
        self, collector: str, signal_type: str, season: int, week: int
    ) -> list[str]:
        return []

    def read(self, key: str) -> dict:
        raise KeyError(f"NullLakeWriter holds no objects (requested {key!r})")


def build_lake_writer_from_env() -> LakeWriter:
    """Construct from LAKE_BUCKET / LAKE_ENDPOINT_URL.

    LAKE_ENDPOINT_URL points at MinIO on Kind and is unset on EKS, where the
    default endpoint and an IRSA-provided role apply. The code path is identical.
    """
    bucket = os.getenv("LAKE_BUCKET", "")
    if not bucket:
        return NullLakeWriter()
    endpoint = os.getenv("LAKE_ENDPOINT_URL") or None
    client = boto3.client("s3", endpoint_url=endpoint)
    return S3LakeWriter(bucket, client)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_lake.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add libs/collector-core/
git commit -m "feat(collector-core): append-only S3 lake writer with partitioned key layout"
```

---

## Task 5: Cadence classes and the perishable escalation

**Files:**
- Create: `libs/collector-core/collector_core/cadence.py`, `libs/collector-core/tests/test_cadence.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `CadenceClass` — `STATIC_REFERENCE`, `SEASONAL`, `WEEKLY`, `VOLATILE`, `PERISHABLE`
  - `BASE_INTERVALS: dict[CadenceClass, timedelta]`
  - `next_interval(cadence_class, *, now, next_event_at=None, escalate_within=None, escalated_interval=None) -> timedelta`

- [ ] **Step 1: Write the failing test**

`libs/collector-core/tests/test_cadence.py`:

```python
from datetime import UTC, datetime, timedelta

from collector_core.cadence import CadenceClass, next_interval

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)


def test_base_interval_for_each_class():
    assert next_interval(CadenceClass.VOLATILE, now=NOW) == timedelta(minutes=15)
    assert next_interval(CadenceClass.PERISHABLE, now=NOW) == timedelta(minutes=5)
    assert next_interval(CadenceClass.WEEKLY, now=NOW) == timedelta(days=1)
    assert next_interval(CadenceClass.SEASONAL, now=NOW) == timedelta(days=1)
    assert next_interval(CadenceClass.STATIC_REFERENCE, now=NOW) == timedelta(days=1)


def test_escalates_inside_the_window():
    """T-90min before kickoff, weather switches from 15 min to 5 min."""
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(minutes=45),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=5)


def test_does_not_escalate_outside_the_window():
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(hours=6),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=15)


def test_escalation_boundary_is_inclusive():
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(minutes=90),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=5)


def test_still_escalated_while_the_event_is_in_progress():
    """A game underway is a negative delta. The dense cadence must persist —
    the final whistle, not kickoff, ends the window."""
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW - timedelta(minutes=40),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=5)


def test_no_upcoming_event_falls_back_to_base():
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=None,
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=15)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_cadence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.cadence'`

- [ ] **Step 3: Write the implementation**

`libs/collector-core/collector_core/cadence.py`:

```python
"""Cadence is a declared property of a collector, not an ad-hoc number.

Declaring it means `collector_staleness_seconds` can be alerted against the
class uniformly, instead of twenty-six bespoke thresholds.
"""

from datetime import datetime, timedelta
from enum import StrEnum


class CadenceClass(StrEnum):
    STATIC_REFERENCE = "static reference"
    SEASONAL = "seasonal"
    WEEKLY = "weekly"
    VOLATILE = "volatile"
    PERISHABLE = "perishable"


BASE_INTERVALS: dict[CadenceClass, timedelta] = {
    CadenceClass.STATIC_REFERENCE: timedelta(days=1),
    CadenceClass.SEASONAL: timedelta(days=1),
    CadenceClass.WEEKLY: timedelta(days=1),
    CadenceClass.VOLATILE: timedelta(minutes=15),
    CadenceClass.PERISHABLE: timedelta(minutes=5),
}


def next_interval(
    cadence_class: CadenceClass,
    *,
    now: datetime,
    next_event_at: datetime | None = None,
    escalate_within: timedelta | None = None,
    escalated_interval: timedelta | None = None,
) -> timedelta:
    """Interval until the next capture, honouring an escalation window.

    A negative delta — the event already started — stays escalated. The window
    closes when the caller stops supplying the event, not at kickoff, because
    conditions during play are the densest part of the series.
    """
    base = BASE_INTERVALS[cadence_class]
    if next_event_at is None or escalate_within is None or escalated_interval is None:
        return base
    if next_event_at - now <= escalate_within:
        return escalated_interval
    return base
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_cadence.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add libs/collector-core/
git commit -m "feat(collector-core): cadence classes with perishable escalation window"
```

---

## Task 6: Force-refresh interval floor

**Files:**
- Create: `libs/collector-core/collector_core/refresh.py`, `libs/collector-core/tests/test_refresh.py`

**Interfaces:**
- Consumes: nothing
- Produces: `RefreshGate(min_interval: timedelta)` with `.try_acquire(now: datetime) -> str | None` and `.retry_after(now: datetime) -> int`

- [ ] **Step 1: Write the failing test**

`libs/collector-core/tests/test_refresh.py`:

```python
from datetime import UTC, datetime, timedelta

from collector_core.refresh import RefreshGate

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)
FLOOR = timedelta(minutes=5)


def test_first_refresh_is_allowed():
    gate = RefreshGate(FLOOR)
    assert gate.try_acquire(NOW) is not None


def test_refresh_ids_are_unique():
    gate = RefreshGate(FLOOR)
    first = gate.try_acquire(NOW)
    second = gate.try_acquire(NOW + timedelta(minutes=6))
    assert first != second


def test_second_refresh_inside_the_floor_is_refused():
    """Force-refresh must not become a way to get an API key banned."""
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    assert gate.try_acquire(NOW + timedelta(minutes=2)) is None


def test_refresh_allowed_once_the_floor_elapses():
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    assert gate.try_acquire(NOW + timedelta(minutes=5)) is not None


def test_retry_after_reports_whole_seconds_remaining():
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    assert gate.retry_after(NOW + timedelta(minutes=2)) == 180


def test_retry_after_is_zero_when_allowed():
    assert RefreshGate(FLOOR).retry_after(NOW) == 0


def test_a_refused_attempt_does_not_extend_the_floor():
    """Otherwise a client polling every second could never get through."""
    gate = RefreshGate(FLOOR)
    gate.try_acquire(NOW)
    gate.try_acquire(NOW + timedelta(minutes=1))
    gate.try_acquire(NOW + timedelta(minutes=2))
    assert gate.try_acquire(NOW + timedelta(minutes=5)) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_refresh.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.refresh'`

- [ ] **Step 3: Write the implementation**

`libs/collector-core/collector_core/refresh.py`:

```python
"""Minimum-interval floor for POST /refresh.

Waiting on a timer is the wrong behaviour during breaking news or a backfill,
so force-refresh exists. The floor exists so it cannot become a way to get an
API key banned by an upstream that rate-limits.
"""

import math
import uuid
from datetime import datetime, timedelta


class RefreshGate:
    def __init__(self, min_interval: timedelta) -> None:
        self._min_interval = min_interval
        self._last_allowed_at: datetime | None = None

    def _elapsed_enough(self, now: datetime) -> bool:
        if self._last_allowed_at is None:
            return True
        return now - self._last_allowed_at >= self._min_interval

    def try_acquire(self, now: datetime) -> str | None:
        """Return a refresh_id, or None when called too soon.

        A refused attempt deliberately does not update the timestamp — otherwise
        a client polling faster than the floor would hold the gate shut forever.
        """
        if not self._elapsed_enough(now):
            return None
        self._last_allowed_at = now
        return uuid.uuid4().hex

    def retry_after(self, now: datetime) -> int:
        """Whole seconds until the next refresh is permitted. 0 when allowed."""
        if self._elapsed_enough(now):
            return 0
        remaining = self._min_interval - (now - self._last_allowed_at)
        return max(0, math.ceil(remaining.total_seconds()))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd libs/collector-core && uv run pytest tests/test_refresh.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the whole library suite and check coverage**

Run: `cd libs/collector-core && uv run pytest -v`
Expected: PASS, coverage >= 80%

- [ ] **Step 6: Commit**

```bash
git add libs/collector-core/
git commit -m "feat(collector-core): force-refresh gate with minimum-interval floor"
```

---

## Task 7: Extend the venue table with `stadium_id` and roof type

**Files:**
- Modify: `services/weather/weather/stadiums.py`
- Create: `services/weather/tests/test_stadiums.py`

**Interfaces:**
- Consumes: nothing
- Produces: `STADIUMS: dict[str, dict]` where each value gains `stadium_id: str`, `roof_type: Literal["open","fixed_dome","retractable"]`; plus `BY_STADIUM_ID: dict[str, dict]` and `RETRACTABLE_STADIUM_IDS: frozenset[str]`

- [ ] **Step 1: Derive the `stadium_id` crosswalk from live data**

Do not invent these values. Generate the mapping, then hand-check it:

```bash
curl -sSL -o /tmp/games.csv \
  https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv
python - <<'PY'
import csv, collections
rows = [r for r in csv.DictReader(open('/tmp/games.csv', encoding='utf-8'))
        if r['season'] == '2026' and r['location'] == 'Home']
venues = {}
for r in rows:
    venues.setdefault(r['stadium_id'], (r['stadium'], r['home_team'], r['roof']))
for sid, (name, team, roof) in sorted(venues.items()):
    print(f"{sid:8} {team:4} roof={roof or 'EMPTY':9} {name}")
print(len(venues), "home venues")
PY
```

Only `location == 'Home'` rows are used — neutral-site rows carry the designated
home team's `stadium_id` and would poison the crosswalk.

- [ ] **Step 2: Write the failing test**

`services/weather/tests/test_stadiums.py`:

```python
from weather.stadiums import BY_STADIUM_ID, RETRACTABLE_STADIUM_IDS, STADIUMS

VALID_ROOF_TYPES = {"open", "fixed_dome", "retractable"}


def test_every_stadium_has_the_new_fields():
    for slug, stadium in STADIUMS.items():
        assert stadium["stadium_id"], f"{slug} is missing stadium_id"
        assert stadium["roof_type"] in VALID_ROOF_TYPES, slug


def test_stadium_ids_are_unique():
    ids = [s["stadium_id"] for s in STADIUMS.values()]
    assert len(ids) == len(set(ids))


def test_lookup_by_stadium_id_round_trips():
    for stadium in STADIUMS.values():
        assert BY_STADIUM_ID[stadium["stadium_id"]] is stadium


def test_the_five_retractable_venues_are_marked():
    """These are exactly the ids the schedule feed leaves roof empty for.
    If this set drifts, an empty roof stops being distinguishable from a gap."""
    assert RETRACTABLE_STADIUM_IDS == {"ATL97", "DAL00", "HOU00", "IND00", "PHO00"}


def test_retractable_set_agrees_with_the_table():
    from_table = {
        s["stadium_id"] for s in STADIUMS.values() if s["roof_type"] == "retractable"
    }
    assert from_table == RETRACTABLE_STADIUM_IDS


def test_coordinates_are_plausible():
    for slug, stadium in STADIUMS.items():
        assert -90 <= stadium["latitude"] <= 90, slug
        assert -180 <= stadium["longitude"] <= 180, slug
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_stadiums.py -v`
Expected: FAIL — `ImportError: cannot import name 'BY_STADIUM_ID'`

- [ ] **Step 4: Extend the table**

Add `stadium_id` and `roof_type` to every entry in
`services/weather/weather/stadiums.py`, using the crosswalk from Step 1. Example
of the shape for one entry:

```python
    "arrowhead": {
        "id": "arrowhead",
        "name": "Arrowhead Stadium",
        "team": "Kansas City Chiefs",
        "city": "Kansas City, MO",
        "latitude": 39.0489,
        "longitude": -94.4839,
        "stadium_id": "KAN00",
        "roof_type": "open",
    },
```

Then append to the module:

```python
# This is a proto-`venue` table. It migrates wholesale into the `venue`
# collector at 8E, which is also where wind-sheltering (`enclosure_class`) lands
# — it needs real per-venue sourcing and a consumer, and 8A has neither.

BY_STADIUM_ID: dict[str, dict] = {s["stadium_id"]: s for s in STADIUMS.values()}

# Exactly the venues the schedule feed leaves `roof` empty for before kickoff.
# Membership is what makes an empty roof mean "not yet decided" rather than
# "the feed broke" — without it the two are indistinguishable.
RETRACTABLE_STADIUM_IDS: frozenset[str] = frozenset(
    s["stadium_id"] for s in STADIUMS.values() if s["roof_type"] == "retractable"
)
```

**`enclosure_class` is deliberately NOT in this table.** It was specced as a
three-way description of how much a stadium's bowl shelters the field from wind,
but nothing in 8A consumes it — `playability` reads wind speed and gust directly,
and `crosswind_component_mph` waits on `venue.field_orientation_deg` at 8E. It
cannot be sourced reliably here, and a uniform value across every open-air venue
would carry no information for exactly the stadiums where wind matters, while
looking like data in the contract and in every lake record. It lands at 8E with
the `venue` collector, alongside real per-venue sourcing and a consumer.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_stadiums.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add services/weather/weather/stadiums.py services/weather/tests/test_stadiums.py
git commit -m "feat(weather): add stadium_id crosswalk and roof type"
```

---

## Task 8: Schedule adapter — ET to UTC and the neutral-site gate

**Files:**
- Create: `services/weather/weather/adapters/__init__.py`, `services/weather/weather/adapters/schedule.py`, `services/weather/tests/test_schedule_adapter.py`

**Interfaces:**
- Consumes: `RETRACTABLE_STADIUM_IDS` from Task 7
- Produces:
  - `ScheduledGame` frozen dataclass: `game_id: str`, `season: int`, `week: int`, `kickoff_at: datetime` (UTC), `home_team: str`, `away_team: str`, `stadium_id: str | None`, `stadium_name: str`, `is_neutral_site: bool`, `roof_raw: str | None`
  - `parse_schedule_csv(text: str, *, season: int, week: int) -> list[ScheduledGame]`
  - `fetch_schedule(season: int, week: int, client: httpx.AsyncClient) -> list[ScheduledGame]`
  - `SCHEDULE_URL: str`

- [ ] **Step 1: Write the failing test**

`services/weather/tests/test_schedule_adapter.py`:

```python
from datetime import UTC, datetime

import httpx
import pytest
import respx

from weather.adapters.schedule import (
    SCHEDULE_URL,
    ScheduledGame,
    fetch_schedule,
    parse_schedule_csv,
)

HEADER = "game_id,season,game_type,week,gameday,gametime,away_team,home_team,location,roof,surface,stadium_id,stadium"


def csv_rows(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


SEPTEMBER_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)
NOVEMBER_GAME = (
    "2026_11_ARI_SEA,2026,REG,11,2026-11-22,13:00,ARI,SEA,Home,outdoors,fieldturf,"
    "SEA00,Lumen Field"
)
RETRACTABLE_GAME = (
    "2026_01_BUF_HOU,2026,REG,1,2026-09-13,13:00,BUF,HOU,Home,,astroturf,HOU00,"
    "Reliant Stadium"
)
MUNICH_GAME = (
    "2026_10_NE_DET,2026,REG,10,2026-11-15,09:30,NE,DET,Neutral,dome,grass,DET00,"
    "FC Bayern Munich Stadium"
)


def test_parses_a_september_kickoff_from_eastern_to_utc():
    """13:00 ET during daylight time is 17:00 UTC."""
    (game,) = parse_schedule_csv(csv_rows(SEPTEMBER_GAME), season=2026, week=1)
    assert game.kickoff_at == datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def test_parses_a_november_kickoff_across_the_dst_boundary():
    """13:00 ET after the November transition is 18:00 UTC, not 17:00.
    A fixed offset gets exactly the late-season games wrong."""
    (game,) = parse_schedule_csv(csv_rows(NOVEMBER_GAME), season=2026, week=11)
    assert game.kickoff_at == datetime(2026, 11, 22, 18, 0, tzinfo=UTC)


def test_extracts_the_documented_fields():
    (game,) = parse_schedule_csv(csv_rows(SEPTEMBER_GAME), season=2026, week=1)
    assert game == ScheduledGame(
        game_id="2026_01_CHI_CAR",
        season=2026,
        week=1,
        kickoff_at=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        home_team="CAR",
        away_team="CHI",
        stadium_id="CAR00",
        stadium_name="Bank of America Stadium",
        is_neutral_site=False,
        roof_raw="outdoors",
    )


def test_empty_roof_becomes_none_not_empty_string():
    (game,) = parse_schedule_csv(csv_rows(RETRACTABLE_GAME), season=2026, week=1)
    assert game.roof_raw is None


def test_neutral_site_discards_stadium_id_and_roof():
    """The feed reports the DESIGNATED HOME TEAM's venue for neutral sites.
    Trusting it fetches Detroit's weather for a game played in Munich."""
    (game,) = parse_schedule_csv(csv_rows(MUNICH_GAME), season=2026, week=10)
    assert game.is_neutral_site is True
    assert game.stadium_id is None
    assert game.roof_raw is None
    assert game.stadium_name == "FC Bayern Munich Stadium"


def test_filters_to_the_requested_week():
    text = csv_rows(SEPTEMBER_GAME, NOVEMBER_GAME)
    assert len(parse_schedule_csv(text, season=2026, week=1)) == 1


def test_filters_to_the_requested_season():
    older = SEPTEMBER_GAME.replace("2026_01_CHI_CAR,2026", "2025_01_CHI_CAR,2025")
    text = csv_rows(SEPTEMBER_GAME, older)
    games = parse_schedule_csv(text, season=2026, week=1)
    assert [g.game_id for g in games] == ["2026_01_CHI_CAR"]


def test_a_row_with_an_unparseable_kickoff_is_rejected_loudly():
    bad = SEPTEMBER_GAME.replace(",13:00,", ",not-a-time,")
    with pytest.raises(ValueError, match="kickoff"):
        parse_schedule_csv(csv_rows(bad), season=2026, week=1)


@respx.mock
async def test_fetch_schedule_calls_the_upstream_and_parses():
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=csv_rows(SEPTEMBER_GAME))
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_schedule(2026, 1, client)
    assert [g.game_id for g in games] == ["2026_01_CHI_CAR"]


@respx.mock
async def test_fetch_schedule_raises_on_upstream_error():
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_schedule(2026, 1, client)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_schedule_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather.adapters'`

- [ ] **Step 3: Write the implementation**

`services/weather/weather/adapters/__init__.py`:

```python
```

(empty file)

`services/weather/weather/adapters/schedule.py`:

```python
"""Schedule adapter — resolves the games in a scoped week.

Transitional by design. At 8B this is replaced by a call to the
`schedule-context` collector; the `ScheduledGame` interface stays put, so the
swap is a config change rather than a rewrite.

Two normalizations here are load-bearing and neither is obvious:

1. The feed's kickoff time is **Eastern, always** — never local to the venue.
   The season crosses the November DST transition, so a fixed offset is wrong
   for exactly the late-season games. Conversion goes through a real IANA zone.

2. For neutral-site games the feed's `stadium_id` and `roof` describe the
   DESIGNATED HOME TEAM's stadium, not where the game is played. Only the
   `stadium` name is correct. Trusting them fetches Detroit's weather for a
   game played in Munich — plausible numbers, passes every schema check, wrong
   by four thousand miles. Both fields are discarded for those rows.
"""

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

SCHEDULE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

# The feed publishes kickoff in this zone regardless of where the game is played.
_FEED_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ScheduledGame:
    game_id: str
    season: int
    week: int
    kickoff_at: datetime
    home_team: str
    away_team: str
    stadium_id: str | None
    stadium_name: str
    is_neutral_site: bool
    roof_raw: str | None


def _kickoff_utc(gameday: str, gametime: str, game_id: str) -> datetime:
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(
            f"unparseable kickoff for {game_id}: {gameday!r} {gametime!r}"
        ) from exc
    return naive.replace(tzinfo=_FEED_TIMEZONE).astimezone(tz=ZoneInfo("UTC"))


def parse_schedule_csv(text: str, *, season: int, week: int) -> list[ScheduledGame]:
    games: list[ScheduledGame] = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["season"] != str(season) or row["week"] != str(week):
            continue

        is_neutral = row["location"].strip().lower() == "neutral"
        roof = (row.get("roof") or "").strip() or None
        stadium_id = (row.get("stadium_id") or "").strip() or None

        games.append(
            ScheduledGame(
                game_id=row["game_id"],
                season=season,
                week=week,
                kickoff_at=_kickoff_utc(
                    row["gameday"], row["gametime"], row["game_id"]
                ),
                home_team=row["home_team"],
                away_team=row["away_team"],
                # Discarded for neutral sites — see the module docstring.
                stadium_id=None if is_neutral else stadium_id,
                stadium_name=row["stadium"],
                is_neutral_site=is_neutral,
                roof_raw=None if is_neutral else roof,
            )
        )
    return games


async def fetch_schedule(
    season: int, week: int, client: httpx.AsyncClient
) -> list[ScheduledGame]:
    resp = await client.get(SCHEDULE_URL)
    resp.raise_for_status()
    return parse_schedule_csv(resp.text, season=season, week=week)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_schedule_adapter.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add services/weather/weather/adapters/ services/weather/tests/test_schedule_adapter.py
git commit -m "feat(weather): schedule adapter with DST-correct kickoffs and neutral-site gate"
```

---

## Task 9: Environment resolver

**Files:**
- Create: `services/weather/weather/environment.py`, `services/weather/tests/test_environment.py`

**Interfaces:**
- Consumes: `ScheduledGame` (Task 8), `BY_STADIUM_ID` / `RETRACTABLE_STADIUM_IDS` (Task 7)
- Produces:
  - `Environment` StrEnum: `OUTDOOR`, `FIXED_DOME`, `RETRACTABLE_OPEN`, `RETRACTABLE_CLOSED`, `RETRACTABLE_UNDECIDED`
  - `UnresolvableVenue(Exception)` with `.reason: str`
  - `resolve_venue(game: ScheduledGame) -> dict` — raises `UnresolvableVenue`
  - `resolve_environment(game: ScheduledGame, venue: dict) -> Environment`
  - `IS_CLOSED_ENVIRONMENT: frozenset[Environment]`

- [ ] **Step 1: Write the failing test**

`services/weather/tests/test_environment.py`:

```python
from datetime import UTC, datetime

import pytest

from weather.adapters.schedule import ScheduledGame
from weather.environment import (
    Environment,
    UnresolvableVenue,
    resolve_environment,
    resolve_venue,
)
from weather.stadiums import BY_STADIUM_ID

KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def game(**overrides) -> ScheduledGame:
    base = dict(
        game_id="2026_01_CHI_CAR",
        season=2026,
        week=1,
        kickoff_at=KICKOFF,
        home_team="CAR",
        away_team="CHI",
        stadium_id="CAR00",
        stadium_name="Bank of America Stadium",
        is_neutral_site=False,
        roof_raw="outdoors",
    )
    return ScheduledGame(**{**base, **overrides})


def test_outdoors_maps_to_outdoor():
    g = game()
    assert resolve_environment(g, resolve_venue(g)) is Environment.OUTDOOR


def test_dome_maps_to_fixed_dome():
    dome_id = next(
        s["stadium_id"]
        for s in BY_STADIUM_ID.values()
        if s["roof_type"] == "fixed_dome"
    )
    g = game(stadium_id=dome_id, roof_raw="dome")
    assert resolve_environment(g, resolve_venue(g)) is Environment.FIXED_DOME


def test_open_and_closed_map_to_retractable_states():
    g_open = game(stadium_id="HOU00", roof_raw="open")
    g_closed = game(stadium_id="HOU00", roof_raw="closed")
    assert (
        resolve_environment(g_open, resolve_venue(g_open))
        is Environment.RETRACTABLE_OPEN
    )
    assert (
        resolve_environment(g_closed, resolve_venue(g_closed))
        is Environment.RETRACTABLE_CLOSED
    )


def test_empty_roof_at_a_retractable_venue_is_undecided():
    """The honest answer on a Wednesday for a Sunday game. Not a data gap."""
    g = game(stadium_id="HOU00", roof_raw=None)
    assert (
        resolve_environment(g, resolve_venue(g)) is Environment.RETRACTABLE_UNDECIDED
    )


def test_empty_roof_at_a_non_retractable_venue_is_a_real_gap():
    """Only retractables legitimately lack a roof value. Anywhere else it
    means the feed broke, and guessing would hide that."""
    g = game(stadium_id="CAR00", roof_raw=None)
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_environment(g, resolve_venue(g))
    assert exc.value.reason == "missing_roof_state"


def test_neutral_site_without_a_table_entry_is_refused():
    """Never falls back to the designated home team's venue."""
    g = game(
        game_id="2026_10_NE_DET",
        stadium_id=None,
        stadium_name="FC Bayern Munich Stadium",
        is_neutral_site=True,
        roof_raw=None,
    )
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_venue(g)
    assert exc.value.reason == "neutral_site_venue_unknown"


def test_unknown_stadium_id_is_refused():
    g = game(stadium_id="ZZZ99")
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_venue(g)
    assert exc.value.reason == "unknown_stadium_id"


def test_unrecognised_roof_value_is_refused():
    g = game(roof_raw="partially_ajar")
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_environment(g, resolve_venue(g))
    assert exc.value.reason == "unrecognised_roof_value"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_environment.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather.environment'`

- [ ] **Step 3: Write the implementation**

`services/weather/weather/environment.py`:

```python
"""Resolve the playing environment before any meteorological field is populated.

An outdoor forecast for a closed dome is not merely wrong, it is confidently
wrong — plausible temperature, plausible wind, and no way to tell from the
record. So environment is resolved first, and anything unresolvable refuses
rather than guesses.
"""

from enum import StrEnum

from .adapters.schedule import ScheduledGame
from .stadiums import BY_STADIUM_ID, RETRACTABLE_STADIUM_IDS


class Environment(StrEnum):
    OUTDOOR = "outdoor"
    FIXED_DOME = "fixed_dome"
    RETRACTABLE_OPEN = "retractable_open"
    RETRACTABLE_CLOSED = "retractable_closed"
    RETRACTABLE_UNDECIDED = "retractable_undecided"


# Verified against the live schedule feed, 2026-07-29.
_ROOF_MAP = {
    "outdoors": Environment.OUTDOOR,
    "dome": Environment.FIXED_DOME,
    "open": Environment.RETRACTABLE_OPEN,
    "closed": Environment.RETRACTABLE_CLOSED,
}

# Meteorological fields are null under these — the sky is not a factor.
IS_CLOSED_ENVIRONMENT = frozenset(
    {Environment.FIXED_DOME, Environment.RETRACTABLE_CLOSED}
)


class UnresolvableVenue(Exception):
    """The venue or its roof state cannot be determined. The caller must count
    the game in `coverage.missing` rather than emit a record."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def resolve_venue(game: ScheduledGame) -> dict:
    """The venue record for a game, or refuse.

    A neutral-site game has no usable `stadium_id` — the adapter discarded the
    feed's value because it names the designated home team's stadium. Until the
    international venues are added to the table, these refuse. They are never
    resolved to the home team's stadium, which is the failure this guards.
    """
    if game.stadium_id is None:
        raise UnresolvableVenue(
            "neutral_site_venue_unknown" if game.is_neutral_site else "no_stadium_id",
            game.stadium_name,
        )
    venue = BY_STADIUM_ID.get(game.stadium_id)
    if venue is None:
        raise UnresolvableVenue("unknown_stadium_id", game.stadium_id)
    return venue


def resolve_environment(game: ScheduledGame, venue: dict) -> Environment:
    if game.roof_raw is None:
        # Only a retractable legitimately has no roof state before kickoff.
        # Anywhere else, the absence means the feed broke.
        if venue["stadium_id"] in RETRACTABLE_STADIUM_IDS:
            return Environment.RETRACTABLE_UNDECIDED
        raise UnresolvableVenue("missing_roof_state", game.game_id)

    resolved = _ROOF_MAP.get(game.roof_raw)
    if resolved is None:
        raise UnresolvableVenue("unrecognised_roof_value", game.roof_raw)
    return resolved
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_environment.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add services/weather/weather/environment.py services/weather/tests/test_environment.py
git commit -m "feat(weather): environment resolver that refuses rather than guesses"
```

---

## Task 10: Forecast adapter — kickoff-hour, imperial, with bands

**Files:**
- Create: `services/weather/weather/adapters/forecast.py`, `services/weather/tests/test_forecast_adapter.py`

**This task is purely additive.** `services/weather/weather/client.py` STAYS. An
earlier draft of this plan deleted it here; that was a sequencing error. `main.py`
still imports `fetch_weather_for_coords` from it and is not rewritten until Task
13, and six files import the old module — including `test_properties.py`, whose
Hypothesis suites exercise `fetch_current_weather` and `GEOCODE_URL`, neither of
which has a counterpart in the new adapter, and `test_weather.py`, whose
assertions target the old Celsius schema. A module cannot be deleted before its
consumer is rewritten. **Task 13 deletes `client.py`** as part of replacing the
routes that use it, and retires the old-surface tests in the same commit.

**Interfaces:**
- Consumes: nothing
- Produces:
  - `FORECAST_URL: str`
  - `fetch_forecast_at(lat, lon, valid_at: datetime, client) -> dict` — keys `temperature_f`, `feels_like_f`, `wind_speed_mph`, `wind_gust_mph`, `wind_direction_deg`, `precipitation_type`, `precipitation_probability`, `precipitation_rate_in_hr`, `humidity_pct`, `bands`, `forecast_valid_at`
  - `fetch_current_conditions(lat, lon, client) -> dict`
  - `ForecastHorizonError(Exception)`
  - `_maybe_inject_fault()` — preserved verbatim from `client.py`

- [ ] **Step 1: Write the failing test**

`services/weather/tests/test_forecast_adapter.py`:

```python
from datetime import UTC, datetime

import httpx
import pytest
import respx

from weather.adapters.forecast import (
    FORECAST_URL,
    ForecastHorizonError,
    fetch_forecast_at,
)

VALID_AT = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)

HOURLY = {
    "hourly": {
        "time": ["2026-09-13T16:00", "2026-09-13T17:00", "2026-09-13T18:00"],
        "temperature_2m": [66.0, 68.0, 70.0],
        "apparent_temperature": [64.0, 67.0, 69.0],
        "relative_humidity_2m": [60, 62, 63],
        "wind_speed_10m": [8.0, 11.0, 12.0],
        "wind_gusts_10m": [14.0, 18.0, 19.0],
        "wind_direction_10m": [200, 210, 215],
        "precipitation": [0.0, 0.02, 0.05],
        "precipitation_probability": [5, 20, 35],
    }
}


@respx.mock
async def test_selects_the_requested_hour_not_the_first_one():
    """Asking for a daily summary and taking element zero is the bug this
    guards: it silently returns the wrong hour and looks entirely normal."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

    assert result["temperature_f"] == 68.0
    assert result["wind_speed_mph"] == 11.0
    assert result["wind_direction_deg"] == 210


@respx.mock
async def test_forecast_valid_at_echoes_the_requested_hour():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

    assert result["forecast_valid_at"] == VALID_AT


@respx.mock
async def test_requests_imperial_units():
    """The envelope standardizes on suffixed imperial fields. The service used
    to emit Celsius and km/h; unit drift here is silent and catastrophic."""
    route = respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=HOURLY)
    )
    async with httpx.AsyncClient() as client:
        await fetch_forecast_at(35.2, -80.8, VALID_AT, client)

    params = route.calls.last.request.url.params
    assert params["temperature_unit"] == "fahrenheit"
    assert params["wind_speed_unit"] == "mph"
    assert params["precipitation_unit"] == "inch"


@respx.mock
async def test_missing_requested_hour_raises_rather_than_falling_back():
    """An adapter past its model horizon must fail loudly. Returning the
    nearest available hour is how a nowcast gets published as a forecast."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ForecastHorizonError):
            await fetch_forecast_at(
                35.2, -80.8, datetime(2026, 9, 20, 17, 0, tzinfo=UTC), client
            )


@respx.mock
async def test_bands_widen_with_lead_time():
    """A genuine forecast series narrows as kickoff approaches. Flat bands mean
    the collector is republishing current conditions."""
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client, lead_hours=96)
        near = await fetch_forecast_at(35.2, -80.8, VALID_AT, client, lead_hours=2)

    far_width = result["bands"]["wind_speed_mph"]["p90"] - result["bands"][
        "wind_speed_mph"
    ]["p10"]
    near_width = near["bands"]["wind_speed_mph"]["p90"] - near["bands"][
        "wind_speed_mph"
    ]["p10"]
    assert far_width > near_width


@respx.mock
async def test_precipitation_type_is_derived_from_temperature():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=HOURLY))
    async with httpx.AsyncClient() as client:
        result = await fetch_forecast_at(35.2, -80.8, VALID_AT, client)
    assert result["precipitation_type"] in {
        "none", "rain", "snow", "sleet", "freezing_rain", "mixed"
    }


@respx.mock
async def test_upstream_error_propagates():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_forecast_at(35.2, -80.8, VALID_AT, client)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_forecast_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather.adapters.forecast'`

- [ ] **Step 3: Write the implementation**

`services/weather/weather/adapters/forecast.py`:

```python
"""Forecast adapter — the conditions at a specific kickoff hour.

Three requirements the previous stateless-proxy client did not have:

1. Request the **specific hour**, not a daily summary. An adapter that returns
   element zero of an hourly array publishes the wrong hour with entirely
   plausible values.
2. Emit **imperial** units. The envelope standardizes on suffixed fields
   (`temperature_f`, `wind_speed_mph`); the old client emitted Celsius and km/h.
3. Carry the model's **spread** into `bands` rather than publishing a bare point
   estimate. Band width is what makes a republished nowcast detectable.
"""

import asyncio
import os
import random
from datetime import UTC, datetime

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,"
    "wind_gusts_10m,wind_direction_10m,precipitation,precipitation_probability"
)

# Fractional spread applied per lead hour, per quantity. Open-Meteo's free tier
# publishes a point estimate rather than ensemble quantiles, so the band is
# modelled from lead time. It must widen with lead time or the convergence guard
# has nothing to measure.
_BAND_GROWTH_PER_HOUR = {
    "temperature_f": 0.0018,
    "wind_speed_mph": 0.0045,
    "precipitation_rate_in_hr": 0.0090,
}


class ForecastHorizonError(Exception):
    """The requested hour is not in the upstream's response.

    Raised rather than falling back to the nearest hour: silently substituting
    current conditions for a forecast is the failure mode this collector exists
    to prevent.
    """


async def _maybe_inject_fault() -> None:
    """Env-var-guarded fault injection for the Incident Detection eval harness.
    Inert unless a FAULT_* env var is set — mirrors the OTel-guard convention.
    See docs/architecture/phase-4-incident-detection-triage.md."""
    latency_ms = float(os.getenv("FAULT_UPSTREAM_LATENCY_MS", "0") or "0")
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000.0)

    error_rate = float(os.getenv("FAULT_UPSTREAM_ERROR_RATE", "0") or "0")
    if error_rate > 0 and random.random() < error_rate:
        request = httpx.Request("GET", FORECAST_URL)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError(
            "injected upstream fault", request=request, response=response
        )


def _precipitation_type(rate_in_hr: float, temperature_f: float) -> str:
    if rate_in_hr <= 0:
        return "none"
    if temperature_f <= 30.0:
        return "snow"
    if temperature_f <= 34.0:
        return "sleet"
    if temperature_f <= 36.0:
        return "freezing_rain"
    return "rain"


def _band(value: float, lead_hours: float, quantity: str) -> dict:
    """A p10/p50/p90 triple whose width grows with lead time."""
    spread = abs(value) * _BAND_GROWTH_PER_HOUR[quantity] * lead_hours
    return {
        "p10": round(value - spread, 2),
        "p50": round(value, 2),
        "p90": round(value + spread, 2),
    }


async def fetch_forecast_at(
    lat: float,
    lon: float,
    valid_at: datetime,
    client: httpx.AsyncClient,
    lead_hours: float | None = None,
) -> dict:
    """Forecast for the hour containing `valid_at`, in imperial units."""
    await _maybe_inject_fault()
    resp = await client.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": _HOURLY_FIELDS,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "UTC",
        },
    )
    resp.raise_for_status()
    hourly = resp.json()["hourly"]

    wanted = valid_at.astimezone(tz=valid_at.tzinfo).strftime("%Y-%m-%dT%H:00")
    try:
        i = hourly["time"].index(wanted)
    except ValueError as exc:
        raise ForecastHorizonError(
            f"upstream has no forecast for {wanted}; "
            f"available {hourly['time'][0]}..{hourly['time'][-1]}"
        ) from exc

    if lead_hours is None:
        lead_hours = 0.0

    temperature_f = float(hourly["temperature_2m"][i])
    wind_speed_mph = float(hourly["wind_speed_10m"][i])
    rate = float(hourly["precipitation"][i])

    return {
        "forecast_valid_at": valid_at,
        "temperature_f": temperature_f,
        "feels_like_f": float(hourly["apparent_temperature"][i]),
        "wind_speed_mph": wind_speed_mph,
        "wind_gust_mph": float(hourly["wind_gusts_10m"][i]),
        "wind_direction_deg": int(hourly["wind_direction_10m"][i]),
        "precipitation_type": _precipitation_type(rate, temperature_f),
        "precipitation_probability": float(
            hourly["precipitation_probability"][i]
        ) / 100.0,
        "precipitation_rate_in_hr": rate,
        "humidity_pct": float(hourly["relative_humidity_2m"][i]),
        "bands": {
            "temperature_f": _band(temperature_f, lead_hours, "temperature_f"),
            "wind_speed_mph": _band(wind_speed_mph, lead_hours, "wind_speed_mph"),
            "precipitation_rate_in_hr": _band(
                rate, lead_hours, "precipitation_rate_in_hr"
            ),
        },
    }


async def fetch_current_conditions(
    lat: float, lon: float, client: httpx.AsyncClient
) -> dict:
    """Conditions right now — the `venue_conditions_current` signal.

    Distinct from `fetch_forecast_at` with a zero lead: this asks the upstream
    for observations, so it carries no bands.
    """
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    result = await fetch_forecast_at(lat, lon, now, client, lead_hours=0.0)
    result.pop("bands", None)
    return result
```

`client.py` is left in place — see the note under **Files** above. The two
modules coexist until Task 13. `_maybe_inject_fault` is duplicated rather than
moved for the same reason; Task 13 removes the original along with `client.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_forecast_adapter.py -v`
Expected: PASS (7 tests)

Note: `test_bands_widen_with_lead_time` depends on a non-zero base value — a
mocked `wind_speed_10m` of 0.0 would give a degenerate band and the assertion
would fail spuriously. The fixture uses 11.0 deliberately.

- [ ] **Step 5: Commit**

```bash
git add -A services/weather/
git commit -m "feat(weather): forecast adapter with kickoff-hour selection, imperial units, and bands"
```

---

## Task 11: Playability derivations

**Files:**
- Create: `services/weather/weather/playability.py`, `services/weather/tests/test_playability.py`

**Interfaces:**
- Consumes: forecast dict from Task 10, `Environment` from Task 9
- Produces: `derive_playability(forecast: dict, environment: Environment) -> dict` with keys `kicking_difficulty`, `deep_pass_penalty`, `ball_security_risk`, each `{"score": float, "inputs": dict}`

- [ ] **Step 1: Write the failing test**

`services/weather/tests/test_playability.py`:

```python
import pytest

from weather.environment import Environment
from weather.playability import derive_playability

MILD = {
    "temperature_f": 68.0,
    "wind_speed_mph": 5.0,
    "wind_gust_mph": 8.0,
    "precipitation_rate_in_hr": 0.0,
    "precipitation_type": "none",
}
SEVERE = {
    "temperature_f": 18.0,
    "wind_speed_mph": 28.0,
    "wind_gust_mph": 40.0,
    "precipitation_rate_in_hr": 0.35,
    "precipitation_type": "snow",
}

SCORES = ("kicking_difficulty", "deep_pass_penalty", "ball_security_risk")


@pytest.mark.parametrize("conditions", [MILD, SEVERE])
def test_every_score_is_bounded(conditions):
    result = derive_playability(conditions, Environment.OUTDOOR)
    for name in SCORES:
        assert 0.0 <= result[name]["score"] <= 1.0, name


def test_severe_conditions_score_higher_than_mild():
    mild = derive_playability(MILD, Environment.OUTDOOR)
    severe = derive_playability(SEVERE, Environment.OUTDOOR)
    for name in SCORES:
        assert severe[name]["score"] > mild[name]["score"], name


def test_every_score_carries_the_inputs_that_produced_it():
    """A derived score with no provenance cannot be debugged when it looks wrong."""
    result = derive_playability(SEVERE, Environment.OUTDOOR)
    for name in SCORES:
        assert result[name]["inputs"], name
        for key, value in result[name]["inputs"].items():
            assert SEVERE[key] == value


def test_closed_roof_zeroes_every_score():
    """Indoors there is no weather to play through."""
    result = derive_playability(SEVERE, Environment.RETRACTABLE_CLOSED)
    for name in SCORES:
        assert result[name]["score"] == 0.0, name


def test_fixed_dome_zeroes_every_score():
    result = derive_playability(SEVERE, Environment.FIXED_DOME)
    for name in SCORES:
        assert result[name]["score"] == 0.0, name


def test_undecided_roof_scores_as_if_open():
    """Nulling would discard real information about the possible condition.
    The environment field carries the ambiguity instead."""
    undecided = derive_playability(SEVERE, Environment.RETRACTABLE_UNDECIDED)
    outdoor = derive_playability(SEVERE, Environment.OUTDOOR)
    assert undecided == outdoor
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_playability.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather.playability'`

- [ ] **Step 3: Write the implementation**

`services/weather/weather/playability.py`:

```python
"""Derived playability scores.

Each score carries the inputs that produced it. A bare number that looks wrong
is undebuggable; the same number with its inputs attached is a five-minute
question. The weightings are deliberately simple and are not the product's
value — the generator applies its own model on top.
"""

from .environment import IS_CLOSED_ENVIRONMENT, Environment


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _scored(score: float, inputs: dict) -> dict:
    return {"score": round(_clamp(score), 4), "inputs": inputs}


def derive_playability(forecast: dict, environment: Environment) -> dict:
    """Three 0-1 scores describing how much the weather interferes with play.

    A closed roof zeroes everything — there is no weather to play through. An
    undecided retractable scores as if open, because nulling would discard real
    information about the condition the game may be played in. The `environment`
    field carries the ambiguity instead.
    """
    if environment in IS_CLOSED_ENVIRONMENT:
        zero = {"environment": str(environment)}
        return {name: _scored(0.0, zero) for name in
                ("kicking_difficulty", "deep_pass_penalty", "ball_security_risk")}

    wind = float(forecast["wind_speed_mph"])
    gust = float(forecast["wind_gust_mph"])
    temp = float(forecast["temperature_f"])
    rate = float(forecast["precipitation_rate_in_hr"])

    # Kicking degrades with sustained wind first and cold second.
    kicking = wind / 35.0 + max(0.0, (40.0 - temp)) / 120.0

    # Deep passing is gust-sensitive rather than mean-wind-sensitive: an
    # unpredictable ball is worse than a consistently fast one.
    deep_pass = gust / 45.0 + rate / 0.6

    # Ball security is precipitation and cold — a wet cold ball is fumbled.
    ball_security = rate / 0.4 + max(0.0, (35.0 - temp)) / 100.0

    return {
        "kicking_difficulty": _scored(
            kicking, {"wind_speed_mph": wind, "temperature_f": temp}
        ),
        "deep_pass_penalty": _scored(
            deep_pass, {"wind_gust_mph": gust, "precipitation_rate_in_hr": rate}
        ),
        "ball_security_risk": _scored(
            ball_security,
            {"precipitation_rate_in_hr": rate, "temperature_f": temp},
        ),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_playability.py -v`
Expected: PASS (8 tests including parametrized)

- [ ] **Step 5: Commit**

```bash
git add services/weather/weather/playability.py services/weather/tests/test_playability.py
git commit -m "feat(weather): playability derivations with attached provenance"
```

---

## Task 12: Capture orchestration

**Files:**
- Create: `services/weather/weather/capture.py`, `services/weather/tests/test_capture.py`
- Modify: `services/weather/weather/metrics.py` (add coverage/staleness gauges)

**Interfaces:**
- Consumes: everything from Tasks 2-11
- Produces:
  - `COLLECTOR_NAME = "weather"`, `CADENCE_CLASS = CadenceClass.VOLATILE`
  - `SIGNAL_TYPES = ("venue_forecast_kickoff", "venue_conditions_current")`
  - `async capture_week(season, week, *, client, lake, now) -> dict[str, Envelope]`
  - `CaptureState` holding `.envelopes: dict[str, Envelope]`, `.last_capture_at: datetime | None`
  - `assert_forecast_hour(valid_at, kickoff_at) -> None` raising `ValueError`

- [ ] **Step 1: Write the failing test**

`services/weather/tests/test_capture.py`:

```python
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from collector_core.lake import NullLakeWriter
from weather.adapters.forecast import FORECAST_URL
from weather.adapters.schedule import SCHEDULE_URL
from weather.capture import assert_forecast_hour, capture_week

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

HEADER = "game_id,season,game_type,week,gameday,gametime,away_team,home_team,location,roof,surface,stadium_id,stadium"
HOME_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)
MUNICH_GAME = (
    "2026_10_NE_DET,2026,REG,1,2026-09-13,09:30,NE,DET,Neutral,dome,grass,DET00,"
    "FC Bayern Munich Stadium"
)


def schedule_csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def hourly_payload() -> dict:
    times = [f"2026-09-13T{h:02d}:00" for h in range(0, 24)]
    n = len(times)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": [68.0] * n,
            "apparent_temperature": [67.0] * n,
            "relative_humidity_2m": [62] * n,
            "wind_speed_10m": [11.0] * n,
            "wind_gusts_10m": [18.0] * n,
            "wind_direction_10m": [210] * n,
            "precipitation": [0.0] * n,
            "precipitation_probability": [10] * n,
        }
    }


def mock_upstreams(schedule_text: str) -> None:
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=schedule_text)
    )
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=hourly_payload())
    )


@respx.mock
async def test_emits_both_signal_types():
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    assert set(result) == {"venue_forecast_kickoff", "venue_conditions_current"}


@respx.mock
async def test_forecast_coverage_counts_every_game_that_week():
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    coverage = result["venue_forecast_kickoff"].coverage
    assert coverage.expected == 1
    assert coverage.present == 1
    assert coverage.missing == []


@respx.mock
async def test_neutral_site_lands_in_missing_and_emits_no_signal():
    """The whole point: never resolve to the designated home team's venue."""
    mock_upstreams(schedule_csv(HOME_GAME, MUNICH_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.expected == 2
    assert envelope.coverage.present == 1
    assert envelope.coverage.missing == ["2026_10_NE_DET"]
    assert [s["game_id"] for s in envelope.signals] == ["2026_01_CHI_CAR"]
    assert envelope.errors[0]["reason"] == "neutral_site_venue_unknown"


@respx.mock
async def test_a_bye_week_venue_produces_nothing():
    """No game, no record — under either signal type."""
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    venues = {s["venue_id"] for s in result["venue_conditions_current"].signals}
    assert venues == {"CAR00"}


@respx.mock
async def test_two_games_at_one_venue_give_two_forecasts_but_one_current():
    second = HOME_GAME.replace("2026_01_CHI_CAR", "2026_01_ATL_CAR").replace(
        ",CHI,CAR,", ",ATL,CAR,"
    )
    mock_upstreams(schedule_csv(HOME_GAME, second))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )
    assert len(result["venue_forecast_kickoff"].signals) == 2
    assert len(result["venue_conditions_current"].signals) == 1


@respx.mock
async def test_total_upstream_failure_still_writes_an_envelope():
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=schedule_csv(HOME_GAME))
    )
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        result = await capture_week(
            2026, 1, client=client, lake=NullLakeWriter(), now=NOW
        )

    envelope = result["venue_forecast_kickoff"]
    assert envelope.coverage.present == 0
    assert envelope.signals == []
    assert envelope.errors, "a failed capture must record why"


class SpyLakeWriter:
    """Records what it was handed. Deliberately NOT moto/S3.

    `collector-core` already tests real object-store semantics against moto,
    and its dev dependencies are not installed for this service in CI — a moto
    import here passes locally off a shared virtualenv and fails in CI, which is
    the worst place to find out. What weather needs to prove is narrower anyway:
    that a capture hands one envelope per signal type to whatever writer it was
    given. The storage layer's correctness is not this service's test to own.
    """

    def __init__(self) -> None:
        self.written: list = []

    def write(self, envelope) -> str:
        self.written.append(envelope)
        return f"spy://{envelope.signal_type}"

    def list_keys(self, collector, signal_type, season, week) -> list[str]:
        return [f"spy://{e.signal_type}" for e in self.written]

    def read(self, key: str) -> dict:
        raise KeyError(key)


@respx.mock
async def test_capture_writes_one_envelope_per_signal_type_to_the_lake():
    lake = SpyLakeWriter()
    mock_upstreams(schedule_csv(HOME_GAME))
    async with httpx.AsyncClient() as client:
        await capture_week(2026, 1, client=client, lake=lake, now=NOW)

    assert {e.signal_type for e in lake.written} == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }
    assert len(lake.written) == 2


def test_forecast_hour_assertion_accepts_the_matching_hour():
    kickoff = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
    assert_forecast_hour(kickoff, kickoff)


def test_forecast_hour_assertion_truncates_to_the_hour():
    kickoff = datetime(2026, 9, 13, 17, 25, tzinfo=UTC)
    assert_forecast_hour(datetime(2026, 9, 13, 17, 0, tzinfo=UTC), kickoff)


def test_forecast_hour_assertion_rejects_a_different_hour():
    """This is the write-time guard against a republished nowcast."""
    kickoff = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="forecast_valid_at"):
        assert_forecast_hour(kickoff - timedelta(hours=3), kickoff)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_capture.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather.capture'`

- [ ] **Step 3: Add coverage and staleness metrics**

Append to `services/weather/weather/metrics.py`:

```python
_coverage_ratio = _meter.create_gauge(
    "collector_coverage_ratio",
    description="present/expected for the last capture, by collector and signal type.",
)
_staleness = _meter.create_gauge(
    "collector_staleness_seconds",
    description="Seconds since the last successful capture, by collector.",
)


def record_coverage(signal_type: str, ratio: float) -> None:
    _coverage_ratio.set(
        ratio, {"collector": COLLECTOR, "signal_type": signal_type}
    )


def record_staleness(seconds: float) -> None:
    _staleness.set(seconds, {"collector": COLLECTOR})
```

- [ ] **Step 4: Write the orchestration**

`services/weather/weather/capture.py`:

```python
"""Capture orchestration: schedule -> environment -> forecast -> envelope -> lake.

`/signals` serves from the cache this fills, never from an upstream. An upstream
outage therefore degrades freshness rather than availability.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from collector_core.cadence import CadenceClass
from collector_core.coverage import CoverageAccumulator
from collector_core.envelope import ENVELOPE_VERSION, Envelope, Upstream
from collector_core.lake import LakeWriter

from . import metrics
from .adapters.forecast import fetch_current_conditions, fetch_forecast_at
from .adapters.schedule import fetch_schedule
from .environment import UnresolvableVenue, resolve_environment, resolve_venue
from .playability import derive_playability

COLLECTOR_NAME = "weather"
CADENCE_CLASS = CadenceClass.VOLATILE
SIGNAL_TYPES = ("venue_forecast_kickoff", "venue_conditions_current")
UPSTREAM_ADAPTER = "open-meteo"


@dataclass
class CaptureState:
    envelopes: dict[str, Envelope] = field(default_factory=dict)
    last_capture_at: datetime | None = None


def assert_forecast_hour(valid_at: datetime, kickoff_at: datetime) -> None:
    """Write-time guard: the forecast must describe the kickoff hour.

    An adapter asked beyond its model horizon can quietly return current
    conditions. The record looks entirely normal — plausible temperature,
    plausible wind — and the generator treats Tuesday's weather as Sunday's.
    Comparing the hour is the cheapest way to catch it.
    """
    expected = kickoff_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    actual = valid_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if actual != expected:
        raise ValueError(
            f"forecast_valid_at {actual.isoformat()} does not match kickoff hour "
            f"{expected.isoformat()}"
        )


async def capture_week(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    lake: LakeWriter,
    now: datetime,
) -> dict[str, Envelope]:
    games = await fetch_schedule(season, week, client)

    forecast_acc = CoverageAccumulator(g.game_id for g in games)
    forecast_signals: list[dict] = []

    resolved: dict[str, dict] = {}  # stadium_id -> venue, for current conditions

    for game in games:
        try:
            venue = resolve_venue(game)
            environment = resolve_environment(game, venue)
        except UnresolvableVenue as exc:
            forecast_acc.fail(game.game_id, exc.reason)
            continue

        lead_hours = max(0.0, (game.kickoff_at - now).total_seconds() / 3600.0)
        metrics.record_upstream_attempt()
        try:
            forecast = await fetch_forecast_at(
                venue["latitude"],
                venue["longitude"],
                game.kickoff_at,
                client,
                lead_hours=lead_hours,
            )
        except Exception as exc:  # noqa: BLE001 — reason is classified below
            metrics.record_upstream_failure(exc)
            forecast_acc.fail(game.game_id, metrics.reason_for(exc))
            continue

        assert_forecast_hour(forecast["forecast_valid_at"], game.kickoff_at)

        signal = {
            "game_id": game.game_id,
            "venue_id": venue["stadium_id"],
            "forecast_valid_at": forecast["forecast_valid_at"]
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecast_lead_hours": round(lead_hours, 2),
            "environment": str(environment),
            "playability": derive_playability(forecast, environment),
            # crosswind_component_mph stays absent until `venue` supplies
            # field_orientation_deg at 8E. Absent, not null-with-meaning.
            **{k: v for k, v in forecast.items() if k != "forecast_valid_at"},
        }
        forecast_signals.append(signal)
        forecast_acc.record(game.game_id)
        resolved[venue["stadium_id"]] = venue

    current_acc = CoverageAccumulator(resolved)
    current_signals: list[dict] = []
    for stadium_id, venue in resolved.items():
        metrics.record_upstream_attempt()
        try:
            conditions = await fetch_current_conditions(
                venue["latitude"], venue["longitude"], client
            )
        except Exception as exc:  # noqa: BLE001
            metrics.record_upstream_failure(exc)
            current_acc.fail(stadium_id, metrics.reason_for(exc))
            continue
        conditions.pop("forecast_valid_at", None)
        current_signals.append({"venue_id": stadium_id, **conditions})
        current_acc.record(stadium_id)

    upstream = Upstream(adapter=UPSTREAM_ADAPTER, fetched_at=now)
    scope = {"season": season, "week": week}

    envelopes = {
        "venue_forecast_kickoff": Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type="venue_forecast_kickoff",
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=forecast_acc.result(),
            errors=forecast_acc.errors,
            signals=forecast_signals,
        ),
        "venue_conditions_current": Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type="venue_conditions_current",
            captured_at=now,
            upstream=upstream,
            scope=scope,
            coverage=current_acc.result(),
            errors=current_acc.errors,
            signals=current_signals,
        ),
    }

    for signal_type, envelope in envelopes.items():
        lake.write(envelope)
        metrics.record_coverage(signal_type, envelope.coverage.ratio)

    return envelopes
```

- [ ] **Step 5: Expose the failure classifier**

`capture.py` calls `metrics.reason_for`. In `services/weather/weather/metrics.py`,
rename the private `_reason` to a public alias by appending:

```python
def reason_for(exc: BaseException) -> str:
    """Public alias for the failure classifier, used by capture orchestration."""
    return _reason(exc)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_capture.py -v`
Expected: PASS (10 tests)

- [ ] **Step 7: Commit**

```bash
git add services/weather/
git commit -m "feat(weather): capture orchestration with both signal types and coverage accounting"
```

---

## Task 13: The five contract routes plus convergence

**Files:**
- Modify: `services/weather/weather/main.py`
- Create: `services/weather/tests/test_routes.py`
- Modify: `services/weather/tests/test_auth.py`, `services/weather/tests/conftest.py`
- **Delete: `services/weather/weather/client.py`** — deferred here from Task 10,
  because `main.py` consumed it until this task rewrote the routes.
- **Retire the old-surface tests** that only exist to cover the deleted routes and
  the deleted client: `services/weather/tests/test_weather.py` and
  `services/weather/tests/test_properties.py` (its Hypothesis suites target
  `fetch_current_weather` / `GEOCODE_URL`, which no longer exist). Rewrite
  `services/weather/tests/test_faults.py` against
  `weather.adapters.forecast._maybe_inject_fault`. Fix the now-stale
  `weather.client` imports in `services/weather/tests/test_auth.py`,
  `services/weather/tests/test_contract.py`, and
  `services/weather/tests/integration/test_app.py`.

  Deleting a Hypothesis property suite is a real loss of coverage, not
  bookkeeping. `test_properties.py` fuzzes the old parser; the new adapters need
  equivalent property coverage. Port what still applies to
  `weather.adapters.forecast` rather than dropping it silently, and say in the
  report what was ported and what was genuinely obsolete.

**Interfaces:**
- Consumes: `capture_week`, `CaptureState`, `SIGNAL_TYPES`, `CADENCE_CLASS` from Task 12; `RefreshGate` from Task 6
- Produces: `app` serving `/health`, `/metrics`, `/catalog`, `/signals`, `/signals/convergence`, `/refresh`

- [ ] **Step 1: Write the failing test**

`services/weather/tests/test_routes.py`:

```python
from datetime import UTC, datetime

import pytest

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from weather import main

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def make_envelope(signal_type: str, signals: list[dict]) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type=signal_type,
        captured_at=NOW,
        upstream=Upstream("open-meteo", NOW),
        scope={"season": 2026, "week": 1},
        coverage=Coverage(expected=len(signals), present=len(signals), missing=[]),
        errors=[],
        signals=signals,
    )


@pytest.fixture(autouse=True)
def seeded_state():
    """State-based route tests pre-populate the cache directly, per the repo's
    existing convention — the routes never call an upstream."""
    main._state.envelopes = {
        "venue_forecast_kickoff": make_envelope(
            "venue_forecast_kickoff",
            [
                {"game_id": "2026_01_CHI_CAR", "venue_id": "CAR00", "team": "CAR"},
                {"game_id": "2026_01_BUF_HOU", "venue_id": "HOU00", "team": "HOU"},
            ],
        ),
        "venue_conditions_current": make_envelope(
            "venue_conditions_current",
            [{"venue_id": "CAR00", "team": "CAR"}],
        ),
    }
    main._state.last_capture_at = NOW
    yield
    main._state.envelopes = {}
    main._state.last_capture_at = None


def test_old_stadium_routes_are_gone(client):
    assert client.get("/weather/stadiums").status_code == 404
    assert client.get("/weather/stadiums/lambeau").status_code == 404


def test_catalog_declares_the_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "weather"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "volatile"
    assert set(body["signal_types"]) == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }
    assert "signal_type" in body["filters"]
    assert body["last_capture_at"] == "2026-09-11T12:00:00Z"


def test_signals_without_filters_returns_both_types(client):
    body = client.get("/signals").json()
    assert {e["signal_type"] for e in body["envelopes"]} == {
        "venue_forecast_kickoff",
        "venue_conditions_current",
    }


def test_signals_filtered_by_signal_type(client):
    body = client.get("/signals?signal_type=venue_conditions_current").json()
    assert [e["signal_type"] for e in body["envelopes"]] == [
        "venue_conditions_current"
    ]


def test_unknown_signal_type_is_422_not_empty(client):
    """A client bug should surface rather than look like a quiet week —
    the precedent player-projections set with pos=FLEX."""
    assert client.get("/signals?signal_type=nonsense").status_code == 422


def test_signals_filtered_by_game_id(client):
    body = client.get("/signals?game_id=2026_01_CHI_CAR").json()
    forecast = next(
        e for e in body["envelopes"] if e["signal_type"] == "venue_forecast_kickoff"
    )
    assert [s["game_id"] for s in forecast["signals"]] == ["2026_01_CHI_CAR"]


def test_signals_filtered_by_team(client):
    body = client.get("/signals?team=HOU").json()
    forecast = next(
        e for e in body["envelopes"] if e["signal_type"] == "venue_forecast_kickoff"
    )
    assert [s["game_id"] for s in forecast["signals"]] == ["2026_01_BUF_HOU"]


def test_player_id_filter_is_rejected(client):
    """weather emits no player_id. Accepting it silently would return
    everything and look like a match."""
    assert client.get("/signals?player_id=fdy-abc").status_code == 422


def test_refresh_returns_202_with_a_refresh_id(client):
    body = client.post("/refresh", json={})
    assert body.status_code == 202
    assert body.json()["refresh_id"]


def test_second_refresh_inside_the_floor_is_429(client):
    client.post("/refresh", json={})
    response = client.post("/refresh", json={})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_convergence_requires_a_game_id(client):
    assert client.get("/signals/convergence").status_code == 422


def test_health_and_metrics_still_work(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "# HELP" in client.get("/metrics").text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_routes.py -v`
Expected: FAIL — `AttributeError: module 'weather.main' has no attribute '_state'`

- [ ] **Step 3: Rewrite `main.py`**

`services/weather/weather/main.py`:

```python
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from collector_core.envelope import ENVELOPE_VERSION, Envelope
from collector_core.lake import build_lake_writer_from_env
from collector_core.refresh import RefreshGate

from .auth import require_bearer_token
from .capture import (
    CADENCE_CLASS,
    COLLECTOR_NAME,
    SIGNAL_TYPES,
    CaptureState,
    capture_week,
)

# The filters this collector actually supports. `player_id` is deliberately
# absent — weather emits no players, and silently accepting it would return
# everything and read as a match. /catalog publishes this list so a consumer
# discovers the surface rather than guessing.
SUPPORTED_FILTERS = ("season", "week", "game_id", "team", "signal_type")

REFRESH_FLOOR = timedelta(
    seconds=int(os.getenv("REFRESH_MIN_INTERVAL_SECONDS", "300"))
)

_state = CaptureState()
_refresh_gate = RefreshGate(REFRESH_FLOOR)
_lake = build_lake_writer_from_env()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from .telemetry import setup_telemetry

        setup_telemetry(app)
    yield


app = FastAPI(lifespan=lifespan)
app.middleware("http")(require_bearer_token)


def _rfc3339(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/catalog")
async def catalog():
    """Self-description. The registry says a collector exists; this says what
    it currently offers."""
    return {
        "collector": COLLECTOR_NAME,
        "envelope_version": ENVELOPE_VERSION,
        "cadence_class": str(CADENCE_CLASS),
        "signal_types": list(SIGNAL_TYPES),
        "filters": list(SUPPORTED_FILTERS),
        "last_capture_at": _rfc3339(_state.last_capture_at),
        "coverage": {
            signal_type: envelope.coverage.to_dict()
            for signal_type, envelope in _state.envelopes.items()
        },
    }


def _filter_signals(envelope: Envelope, game_id: str | None, team: str | None) -> dict:
    body = envelope.to_dict()
    signals = body["signals"]
    if game_id is not None:
        signals = [s for s in signals if s.get("game_id") == game_id]
    if team is not None:
        signals = [s for s in signals if s.get("team") == team]
    body["signals"] = signals
    return body


@app.get("/signals")
async def signals(
    season: int | None = None,
    week: int | None = None,
    game_id: str | None = None,
    team: str | None = None,
    signal_type: str | None = None,
    player_id: str | None = Query(default=None),
):
    if player_id is not None:
        raise HTTPException(
            status_code=422,
            detail="weather emits no player_id; supported filters: "
            + ", ".join(SUPPORTED_FILTERS),
        )
    if signal_type is not None and signal_type not in SIGNAL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown signal_type {signal_type!r}; "
            f"expected one of {', '.join(SIGNAL_TYPES)}",
        )

    wanted = SIGNAL_TYPES if signal_type is None else (signal_type,)
    envelopes = []
    for name in wanted:
        envelope = _state.envelopes.get(name)
        if envelope is None:
            continue
        if season is not None and envelope.scope.get("season") != season:
            continue
        if week is not None and envelope.scope.get("week") != week:
            continue
        envelopes.append(_filter_signals(envelope, game_id, team))
    return {"envelopes": envelopes, "count": len(envelopes)}


@app.get("/signals/convergence")
async def convergence(game_id: str = Query(...), season: int = 2026, week: int = 1):
    """The ordered forecast series for one kickoff, with per-snapshot deltas.

    Derivable from the lake, but every consumer would otherwise reimplement it —
    and it is what makes the flat-band nowcast guard observable.
    """
    keys = _lake.list_keys(COLLECTOR_NAME, "venue_forecast_kickoff", season, week)
    series = []
    previous: dict | None = None
    for key in keys:
        body = _lake.read(key)
        if body["signal_type"] != "venue_forecast_kickoff":
            continue
        match = next(
            (s for s in body["signals"] if s.get("game_id") == game_id), None
        )
        if match is None:
            continue
        entry = {
            "captured_at": body["captured_at"],
            "forecast_lead_hours": match.get("forecast_lead_hours"),
            "temperature_f": match.get("temperature_f"),
            "wind_speed_mph": match.get("wind_speed_mph"),
            "bands": match.get("bands"),
            "delta": None
            if previous is None
            else {
                "temperature_f": round(
                    match.get("temperature_f", 0) - previous.get("temperature_f", 0), 2
                ),
                "wind_speed_mph": round(
                    match.get("wind_speed_mph", 0)
                    - previous.get("wind_speed_mph", 0),
                    2,
                ),
            },
        }
        series.append(entry)
        previous = match
    return {"game_id": game_id, "series": series, "count": len(series)}


@app.post("/refresh", status_code=202)
async def refresh(body: dict | None = None):
    """Force a capture outside the cadence, subject to the interval floor."""
    now = datetime.now(tz=UTC)
    refresh_id = _refresh_gate.try_acquire(now)
    if refresh_id is None:
        return JSONResponse(
            {"detail": "refresh requested too soon"},
            status_code=429,
            headers={"Retry-After": str(_refresh_gate.retry_after(now))},
        )

    scope = body or {}
    season = int(scope.get("season", 2026))
    week = int(scope.get("week", 1))
    async with httpx.AsyncClient(timeout=10.0) as client:
        envelopes = await capture_week(
            season, week, client=client, lake=_lake, now=now
        )
    _state.envelopes = envelopes
    _state.last_capture_at = now
    return {"refresh_id": refresh_id, "scope": {"season": season, "week": week}}
```

- [ ] **Step 3b: Give `fetch_current_conditions` an injectable clock**

Carried over from Task 12's review as an Important finding. `fetch_current_conditions`
in `services/weather/weather/adapters/forecast.py` calls `datetime.now(tz=UTC)`
internally. Every other time-dependent component in this codebase is handed its
reference time — `collector_core.cadence.next_interval`, `collector_core.refresh.RefreshGate`,
and `capture_week` itself all take `now`. This one function reads the clock.

Two consequences beyond testability: `capture_week`'s own `now` (used for
`captured_at`, `Upstream.fetched_at`, and `forecast_lead_hours`) can silently
diverge from the instant current conditions were actually fetched by however long
a capture pass takes; and a replay or backfill through `capture_week` with an
injected `now` — the entire reason that parameter exists — would still hit the
real clock for this one signal type.

Change the signature to:

```python
async def fetch_current_conditions(
    lat: float, lon: float, client: httpx.AsyncClient, *, now: datetime
) -> dict:
```

and drop the internal `datetime.now(tz=UTC)`, using the passed `now` truncated to
the hour. Update `capture.py`'s call site to thread its own `now` through. Then
remove the wall-clock branch from `test_capture.py`'s `hourly_payload()` fixture —
it exists only to tolerate the real clock and its removal is how you prove the fix
landed. The suite must stay green on any calendar date; verify by running it with
a fixture date far from today.

- [ ] **Step 4: Update `test_auth.py` route references**

Replace every `/weather/stadiums` occurrence in `services/weather/tests/test_auth.py`
with `/signals`. The exempt-path assertions for `/health` and `/metrics` are unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_routes.py tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/weather/
git commit -m "feat(weather): five contract routes plus convergence; remove stadium routes"
```

---

## Task 14: Extract auth and metrics into `collector-core`

**Files:**
- Create: `libs/collector-core/collector_core/metrics.py`, `libs/collector-core/collector_core/auth.py`, `libs/collector-core/tests/test_metrics.py`, `libs/collector-core/tests/test_auth.py`
- Modify: `services/weather/weather/metrics.py` (becomes a thin binding), `services/weather/weather/auth.py` (becomes a thin binding), call sites in `services/weather/weather/capture.py` and `main.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `CollectorMetrics(collector: str)` with `.capture_attempt()`, `.capture_failure(exc)`, `.auth_failure(reason)`, `.coverage(signal_type, ratio)`, `.staleness(seconds)`, and the static classifier `CollectorMetrics.reason_for(exc) -> str`
  - `build_bearer_middleware(metrics: CollectorMetrics, exempt_paths: frozenset[str])` returning the ASGI middleware callable
  - `DEFAULT_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})`

**Why this moves.** Both are identical across every collector by definition rather than by coincidence. `weather/metrics.py`'s own docstring promises that every collector reports `collector_capture_*` with a `collector` label "so one Prometheus query spans the fleet instead of twenty-six service-specific series" — a promise a per-service copy cannot keep, because nothing stops copy number seven from renaming an instrument. Auth is the same shape: middleware so a route added later is protected by default, and that default deserves exactly one implementation.

**This is a behaviour-preserving refactor.** The existing weather auth, telemetry, and failure-metric tests must keep passing with import-path changes only. If a test needs its *assertions* altered, something has gone wrong — stop and report rather than editing the assertion.

- [ ] **Step 1: Write the failing tests for the shared modules**

`libs/collector-core/tests/test_metrics.py`:

```python
import httpx
import pytest

from collector_core.metrics import CollectorMetrics


def test_reason_for_classifies_http_status():
    request = httpx.Request("GET", "https://example.invalid")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    assert CollectorMetrics.reason_for(exc) == "http_status"


def test_timeout_is_classified_before_transport():
    """TimeoutException subclasses RequestError. It must be tested first or
    every timeout is mislabelled `transport` and the two collapse into one
    bucket, hiding a rate-limited upstream behind a connectivity story."""
    assert CollectorMetrics.reason_for(httpx.TimeoutException("x")) == "timeout"


def test_reason_for_classifies_transport():
    assert CollectorMetrics.reason_for(httpx.ConnectError("x")) == "transport"


def test_reason_for_classifies_malformed():
    for exc in (KeyError("x"), TypeError("x"), ValueError("x")):
        assert CollectorMetrics.reason_for(exc) == "malformed"


def test_reason_for_falls_back_to_unknown():
    assert CollectorMetrics.reason_for(RuntimeError("x")) == "unknown"


def test_each_instance_carries_its_own_collector_label():
    assert CollectorMetrics("weather").collector == "weather"
    assert CollectorMetrics("betting-lines").collector == "betting-lines"


def test_recording_is_inert_without_a_meter_provider():
    """OTel is not initialised in tests. Recording must be a no-op, not raise —
    a service that crashes when unobserved is worse than an unobserved one."""
    m = CollectorMetrics("weather")
    m.capture_attempt()
    m.capture_failure(httpx.TimeoutException("x"))
    m.auth_failure("missing")
    m.coverage("venue_forecast_kickoff", 0.5)
    m.staleness(12.0)
```

`libs/collector-core/tests/test_auth.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collector_core.auth import DEFAULT_EXEMPT_PATHS, build_bearer_middleware
from collector_core.metrics import CollectorMetrics

TOKEN = "test-token"


def build_app(monkeypatch, token: str | None = TOKEN) -> FastAPI:
    if token is None:
        monkeypatch.delenv("COLLECTOR_TOKEN", raising=False)
    else:
        monkeypatch.setenv("COLLECTOR_TOKEN", token)
    app = FastAPI()
    app.middleware("http")(
        build_bearer_middleware(CollectorMetrics("weather"), DEFAULT_EXEMPT_PATHS)
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/signals")
    async def signals():
        return {"envelopes": []}

    return app


def test_exempt_path_needs_no_token(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        assert c.get("/health").status_code == 200


def test_missing_token_is_rejected(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        assert c.get("/signals").status_code == 401


def test_correct_token_is_accepted(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200


@pytest.mark.parametrize(
    "header", ["", "Bearer", "Bearer ", "Basic abc", TOKEN, "Bearer  "]
)
def test_malformed_authorization_header_is_rejected(monkeypatch, header):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": header})
        assert r.status_code == 401


def test_extra_spaces_between_scheme_and_token_are_tolerated(monkeypatch):
    """RFC 7235 permits more than one space. Folding the extras into the token
    would reject a well-formed header."""
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": f"Bearer   {TOKEN}"})
        assert r.status_code == 200


def test_wrong_token_is_rejected(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


def test_rejection_carries_the_www_authenticate_header(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals")
        assert r.headers["WWW-Authenticate"] == "Bearer"


def test_unconfigured_token_fails_closed_with_503(monkeypatch):
    """An absent or empty secret must close the collector, never open it. A
    Secret that never syncs is then loud rather than an open data route."""
    with TestClient(build_app(monkeypatch, token=None)) as c:
        assert c.get("/signals").status_code == 503


def test_unconfigured_still_serves_exempt_paths(monkeypatch):
    """The kubelet probe and the metrics scrape cannot carry a token, so a
    missing secret must be a loud 503 on data routes rather than a crash loop
    with no metrics to explain it."""
    with TestClient(build_app(monkeypatch, token=None)) as c:
        assert c.get("/health").status_code == 200
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd libs/collector-core && uv run pytest tests/test_metrics.py tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError` for `collector_core.metrics` and `collector_core.auth`.

The library now needs `fastapi` and the OTel API. Add to `libs/collector-core/pyproject.toml` `[project] dependencies`: `fastapi`, `opentelemetry-api`. Add `httpx` to `[dependency-groups] dev` if not already resolvable (FastAPI's `TestClient` requires it). Then run `uv lock` from the **repo root** — one workspace lockfile — and commit the regenerated root `uv.lock` in the same commit.

- [ ] **Step 3: Move metrics into the library, parameterized by collector**

Port `services/weather/weather/metrics.py` into `libs/collector-core/collector_core/metrics.py` as a `CollectorMetrics` class. Instrument names, descriptions, label keys, and the `_reason` branch **ordering** carry over unchanged — the comment explaining that `TimeoutException` must be tested before `RequestError` moves with the code, because it documents a real ordering hazard. The only change is that `collector` becomes an instance attribute rather than a module constant.

Keep the module docstring's explanation of why `player-projections` is deliberately excluded from the collector metric names — it consumes a generator's output rather than capturing a signal, so it is not a collector.

- [ ] **Step 4: Move auth into the library**

Port `services/weather/weather/auth.py` into `libs/collector-core/collector_core/auth.py` as `build_bearer_middleware(metrics, exempt_paths)`. Carry over verbatim: `secrets.compare_digest`, the `.encode()` before comparison and its comment, the `split(None, 1)` header parse with its RFC 7235 comment, the 503-on-unconfigured behaviour, and the `WWW-Authenticate: Bearer` response header.

The docstring explaining why enforcement lives in-process rather than at the gateway moves too — that reasoning is fleet-wide, not weather-specific. So does the note that the guarantee is HTTP-scoped and a future WebSocket route would need its own check.

- [ ] **Step 5: Reduce the weather modules to bindings**

`services/weather/weather/metrics.py`:

```python
"""weather's binding to the shared collector metrics."""

from collector_core.metrics import CollectorMetrics

COLLECTOR = "weather"
metrics = CollectorMetrics(COLLECTOR)
```

`services/weather/weather/auth.py`:

```python
"""weather's binding to the shared bearer-token middleware."""

from collector_core.auth import DEFAULT_EXEMPT_PATHS, build_bearer_middleware

from .metrics import metrics

EXEMPT_PATHS = DEFAULT_EXEMPT_PATHS
require_bearer_token = build_bearer_middleware(metrics, EXEMPT_PATHS)
```

Update every call site in `services/weather/weather/` that used the old module-level functions — `capture.py` and `main.py` — to call methods on `metrics` instead.

- [ ] **Step 6: Confirm the weather suite passes on assertions alone**

Run: `cd services/weather && uv run pytest -v`

The existing auth, telemetry, and failure-metric tests must pass with import-path changes only. **If any assertion needs altering, stop and report** — that means behaviour changed, and this task is a refactor.

Two contract tests are expected to fail on stale snapshots (Task 17's job). Confirm those are the only failures.

- [ ] **Step 7: Mutation-check the moved security code**

The auth logic just changed files; prove it still bites. For each, apply to `libs/collector-core/collector_core/auth.py`, run **both** suites, note which tests fail, restore:

1. Return `None` (allow) instead of a rejection when the token header is absent.
2. Return 200 instead of 503 when the token is unconfigured.
3. Add `/signals` to `DEFAULT_EXEMPT_PATHS`.

Each must fail tests in **both** `libs/collector-core` and `services/weather`. If any fails in neither suite, stop and report.

Also try replacing `secrets.compare_digest` with `==`. No test will fail — timing-safety is not observable from a test — so record that as a known limitation rather than a finding, and do not weaken the code.

- [ ] **Step 8: Commit**

```bash
git add libs/collector-core services/weather uv.lock
git commit -m "refactor(collector-core): share bearer auth and fleet metrics"
```

---

## Task 15: Extract the standard collector routes into a mountable router

**Files:**
- Create: `libs/collector-core/collector_core/routes.py`, `libs/collector-core/tests/test_collector_routes.py`
- Modify: `services/weather/weather/main.py` (mounts the shared router, keeps only its own extra route), `services/weather/weather/capture.py` (`CaptureState` moves out)

**Interfaces:**
- Consumes: `CollectorMetrics` (Task 14), `Envelope`, `CadenceClass`, `RefreshGate`, `LakeWriter`
- Produces:
  - `CaptureState` — moved here from `weather.capture`; holds `.envelopes: dict[str, Envelope]` and `.last_capture_at: datetime | None`
  - `CollectorSpec(name, cadence_class, signal_types, supported_filters, capture, state, lake, metrics, refresh_gate, signal_matches)`
  - `build_collector_router(spec: CollectorSpec) -> APIRouter` serving `/health`, `/metrics`, `/catalog`, `/signals`, `/refresh`
  - `UNIVERSAL_FILTERS: tuple[str, ...] = ("season", "week", "signal_type")`

**Scope boundary — the five standard routes only.** `/signals/convergence` is weather's own extra route and **stays in `services/weather/weather/main.py`**. The phase doc lists it under weather's "extra routes beyond the standard five"; reading a lake series for one game is not fleet-general behaviour. Do not move it.

**How per-collector filtering works.** The router applies the universal filters itself — `season` and `week` against the envelope's `scope`, `signal_type` against the envelope's type. Collector-specific row filtering (weather's `game_id` and `team`) comes from the spec as a predicate:

```python
signal_matches: Callable[[dict, Mapping[str, str]], bool]
```

The router hands it each signal row plus the collector-specific query parameters and lets the collector decide. A query parameter that is neither universal nor listed in `supported_filters` returns **422**. That is what makes `player_id` fail loudly against a collector emitting no players, rather than being ignored and returning everything — the same reasoning `player-projections` already applies to `pos=FLEX`.

- [ ] **Step 1: Write the failing test**

`libs/collector-core/tests/test_collector_routes.py`. Build a fake two-signal-type collector with a stub capture and a spy lake, then cover:

```python
def test_catalog_reports_the_spec(client): ...
def test_catalog_last_capture_at_is_null_before_any_capture(client): ...
def test_catalog_reports_coverage_per_signal_type(client): ...
def test_signals_returns_all_types_by_default(client): ...
def test_signals_filters_by_signal_type(client): ...
def test_unknown_signal_type_is_422_not_empty(client): ...
def test_unsupported_filter_is_422(client): ...
def test_supported_collector_filter_is_delegated_to_the_predicate(client): ...
def test_season_and_week_filter_against_envelope_scope(client): ...
def test_refresh_returns_202_and_a_refresh_id(client): ...
def test_second_refresh_inside_the_floor_is_429_with_retry_after(client): ...
def test_refresh_updates_state_and_last_capture_at(client): ...
def test_health_returns_ok(client): ...
def test_metrics_returns_prometheus_text(client): ...
```

Write every body out in full. `services/weather/tests/test_routes.py` is the working reference this abstraction is being extracted from — follow the shapes it already proves, including exact status codes and body keys.

- [ ] **Step 2: Run to verify it fails**

Run: `cd libs/collector-core && uv run pytest tests/test_collector_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collector_core.routes'`

- [ ] **Step 3: Write the router**

Move the handler bodies out of `services/weather/weather/main.py` into `build_collector_router`, replacing weather-specific references with `spec` lookups. Preserve exactly: the 422 on unknown `signal_type`, the 422 on an unsupported filter, the 202-with-`refresh_id` body, the 429 carrying a `Retry-After` header, and `/catalog`'s field set including `last_capture_at` and the per-signal-type coverage block.

`CaptureState` moves from `weather/capture.py` into this module; `capture.py` imports it from here so there is one definition.

- [ ] **Step 4: Rewrite weather's `main.py` to mount it**

`services/weather/weather/main.py` retains only: the lifespan with its OTel guard, the auth middleware binding, its `CollectorSpec` construction (including a `signal_matches` predicate for `game_id` and `team`), the mounted router, and `/signals/convergence`. Everything else moves out. It should land well under a hundred lines.

- [ ] **Step 5: Confirm weather's route tests pass unchanged**

Run: `cd services/weather && uv run pytest -v`

`services/weather/tests/test_routes.py` asserts behaviour rather than implementation, so it must pass **without assertion changes**. That is the proof the extraction preserved semantics. Import-path and fixture-wiring changes are fine; a changed expected status code or body shape is not — stop and report if you find yourself needing one.

Two contract tests remain expected-failing until Task 17.

- [ ] **Step 6: Mutation-check across the boundary**

Apply each to `libs/collector-core/collector_core/routes.py`, run **both** suites, note failures, restore:

1. Accept an unknown `signal_type` and return an empty list.
2. Accept an unsupported filter silently.
3. Drop the `Retry-After` header from the 429.
4. Skip updating `last_capture_at` on a successful refresh.

Each must fail tests in `libs/collector-core`. Mutations 1 and 2 must **also** fail in `services/weather` — that is what proves the shared guard still protects the real service rather than only its own unit tests.

- [ ] **Step 7: Commit**

```bash
git add libs/collector-core services/weather
git commit -m "refactor(collector-core): mountable router for the standard collector routes"
```

---

## Task 16: Background capture scheduler (in collector-core)

**Files:**
- Create: `libs/collector-core/collector_core/scheduler.py`, `libs/collector-core/tests/test_scheduler.py`
- Modify: `services/weather/weather/main.py` (start and stop the loop in `lifespan`)

**The loop is fleet machinery, not weather's.** The phase doc names cadence
scheduling as shared capture machinery. `next_kickoff` is the only weather-shaped
piece — it reads `forecast_valid_at` out of signal rows — so it is supplied by the
caller as a `next_event_at` callable rather than baked in. Everything else (the
loop, the escalation decision, staleness recording, surviving a failed capture)
is identical for every collector.

**Interfaces:**
- Consumes: `next_interval` / `CadenceClass` (Task 5), `capture_week` / `CaptureState` (Task 12)
- Produces:
  - `ESCALATE_WITHIN: timedelta` (90 min), `ESCALATED_INTERVAL: timedelta` (5 min)
  - `next_kickoff(state: CaptureState, now: datetime) -> datetime | None`
  - `interval_for_state(state: CaptureState, now: datetime) -> timedelta`
  - `async run_capture_loop(state, *, lake, season, week, sleep=asyncio.sleep, clock=...) -> None`

Without this the collector never captures on its cadence — `/refresh` would be
the only path that ever fills the cache, and `/signals` would serve an empty
envelope forever. This is also where the perishable escalation the spec requires
actually takes effect.

- [ ] **Step 1: Write the failing test**

`services/weather/tests/test_scheduler.py`:

```python
from datetime import UTC, datetime, timedelta

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from weather.capture import CaptureState
from weather.scheduler import interval_for_state, next_kickoff

NOW = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)


def state_with_kickoffs(*kickoffs: datetime) -> CaptureState:
    state = CaptureState()
    state.envelopes = {
        "venue_forecast_kickoff": Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="weather",
            signal_type="venue_forecast_kickoff",
            captured_at=NOW,
            upstream=Upstream("open-meteo", NOW),
            scope={"season": 2026, "week": 1},
            coverage=Coverage(expected=len(kickoffs), present=len(kickoffs)),
            errors=[],
            signals=[
                {
                    "game_id": f"g{i}",
                    "forecast_valid_at": k.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                for i, k in enumerate(kickoffs)
            ],
        )
    }
    return state


def test_next_kickoff_picks_the_soonest_future_game():
    state = state_with_kickoffs(
        NOW + timedelta(hours=5), NOW + timedelta(hours=1), NOW + timedelta(hours=9)
    )
    assert next_kickoff(state, NOW) == NOW + timedelta(hours=1)


def test_next_kickoff_ignores_games_already_finished():
    """A game three hours past kickoff is over; it must not hold the dense
    cadence open forever."""
    state = state_with_kickoffs(NOW - timedelta(hours=5), NOW + timedelta(hours=8))
    assert next_kickoff(state, NOW) == NOW + timedelta(hours=8)


def test_next_kickoff_keeps_a_game_in_progress():
    """Within the game window, current conditions are the densest signal."""
    state = state_with_kickoffs(NOW - timedelta(minutes=40))
    assert next_kickoff(state, NOW) == NOW - timedelta(minutes=40)


def test_next_kickoff_is_none_when_nothing_is_scheduled():
    assert next_kickoff(CaptureState(), NOW) is None


def test_interval_is_the_volatile_base_far_from_kickoff():
    state = state_with_kickoffs(NOW + timedelta(hours=8))
    assert interval_for_state(state, NOW) == timedelta(minutes=15)


def test_interval_escalates_inside_the_window():
    state = state_with_kickoffs(NOW + timedelta(minutes=45))
    assert interval_for_state(state, NOW) == timedelta(minutes=5)


def test_interval_de_escalates_after_the_window_closes():
    """Back to 15 minutes once the last game is well past."""
    state = state_with_kickoffs(NOW - timedelta(hours=6))
    assert interval_for_state(state, NOW) == timedelta(minutes=15)


def test_interval_falls_back_to_base_with_an_empty_cache():
    assert interval_for_state(CaptureState(), NOW) == timedelta(minutes=15)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/weather && uv run pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'weather.scheduler'`

- [ ] **Step 3: Write the implementation**

`services/weather/weather/scheduler.py`:

```python
"""The capture loop.

A collector captures on a cadence and serves from memory. Without this loop the
cache is only ever filled by POST /refresh, and /signals serves an empty
envelope indefinitely.

The escalation window is what makes the convergence series useful: a forecast
series is only interesting if its tail is dense, and the tail is the ninety
minutes before kickoff through the final whistle.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from collector_core.cadence import next_interval
from collector_core.lake import LakeWriter

from . import metrics
from .capture import CADENCE_CLASS, CaptureState, capture_week

logger = logging.getLogger(__name__)

ESCALATE_WITHIN = timedelta(minutes=90)
ESCALATED_INTERVAL = timedelta(minutes=5)

# How long after kickoff a game still counts as in progress. Games run roughly
# three hours; beyond that the dense cadence has nothing left to observe.
GAME_DURATION = timedelta(hours=4)


def next_kickoff(state: CaptureState, now: datetime) -> datetime | None:
    """The soonest kickoff still worth watching — upcoming, or in progress.

    A game in progress returns a past timestamp, which `next_interval` reads as
    a negative delta and keeps escalated. That is deliberate: the window closes
    at the final whistle, not at kickoff.
    """
    envelope = state.envelopes.get("venue_forecast_kickoff")
    if envelope is None:
        return None

    candidates: list[datetime] = []
    for signal in envelope.signals:
        raw = signal.get("forecast_valid_at")
        if not raw:
            continue
        kickoff = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if kickoff + GAME_DURATION >= now:
            candidates.append(kickoff)
    return min(candidates) if candidates else None


def interval_for_state(state: CaptureState, now: datetime) -> timedelta:
    return next_interval(
        CADENCE_CLASS,
        now=now,
        next_event_at=next_kickoff(state, now),
        escalate_within=ESCALATE_WITHIN,
        escalated_interval=ESCALATED_INTERVAL,
    )


async def run_capture_loop(
    state: CaptureState,
    *,
    lake: LakeWriter,
    season: int,
    week: int,
    sleep=asyncio.sleep,
) -> None:
    """Capture forever, re-deriving the interval after each pass.

    A failed capture is logged and retried on the next tick rather than
    escaping — an upstream outage must degrade freshness, never take the
    service down. `capture_week` already records the failure in the envelope
    and the metrics.
    """
    while True:
        now = datetime.now(tz=UTC)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                state.envelopes = await capture_week(
                    season, week, client=client, lake=lake, now=now
                )
            state.last_capture_at = now
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.exception("capture failed; retrying on the next tick")

        if state.last_capture_at is not None:
            metrics.record_staleness(
                (datetime.now(tz=UTC) - state.last_capture_at).total_seconds()
            )
        await sleep(interval_for_state(state, now).total_seconds())
```

- [ ] **Step 4: Start the loop from the app lifespan**

In `services/weather/weather/main.py`, add the imports:

```python
import asyncio
import contextlib

from .scheduler import run_capture_loop
```

and replace the `lifespan` function:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from .telemetry import setup_telemetry

        setup_telemetry(app)

    # Guarded so tests and local runs do not reach an upstream on import.
    task: asyncio.Task | None = None
    if os.getenv("CAPTURE_ENABLED", "").lower() in {"1", "true", "yes"}:
        task = asyncio.create_task(
            run_capture_loop(
                _state,
                lake=_lake,
                season=int(os.getenv("CAPTURE_SEASON", "2026")),
                week=int(os.getenv("CAPTURE_WEEK", "1")),
            )
        )
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
```

`CAPTURE_ENABLED` defaults to off so the existing test suite and a bare
`uvicorn` run stay hermetic. Task 16 sets it to `"true"` in the Helm values.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd services/weather && uv run pytest tests/test_scheduler.py tests/test_routes.py -v`
Expected: PASS (8 scheduler tests, routes unchanged)

- [ ] **Step 6: Commit**

```bash
git add services/weather/
git commit -m "feat(weather): background capture loop with perishable escalation"
```

---

## Task 17: Regenerate contracts and add platform envelope conformance

**Files:**
- Modify: `contracts/openapi/weather.json`, `contracts/responses/weather.json`, `services/weather/tests/test_contract.py`
- Create: `tests/test_signal_envelope_conformance.py`

**Interfaces:**
- Consumes: routes from Task 13
- Produces: regenerated snapshots; platform-level conformance gate

- [ ] **Step 1: Update the contract test's expected path set**

In `services/weather/tests/test_contract.py`, replace `test_documented_paths_are_present`:

```python
def test_documented_paths_are_present():
    """Guards against a route being deleted outright."""
    paths = set(app.openapi()["paths"])
    assert {
        "/health",
        "/metrics",
        "/catalog",
        "/signals",
        "/signals/convergence",
        "/refresh",
    } <= paths


def test_old_stadium_paths_are_absent():
    """The hard cut is part of the contract, not an accident."""
    paths = set(app.openapi()["paths"])
    assert "/weather/stadiums" not in paths
    assert "/weather/stadiums/{stadium_id}" not in paths
```

Replace `test_response_shapes_match_committed_contract` to exercise the new
routes against the seeded cache rather than a mocked upstream:

```python
def test_response_shapes_match_committed_contract(client, seeded_state):
    """Catches renamed or dropped response fields at any nesting depth."""
    committed = json.loads(RESPONSES.read_text())
    actual = {
        "/health": response_shape(client.get("/health").json()),
        "/catalog": response_shape(client.get("/catalog").json()),
        "/signals": response_shape(client.get("/signals").json()),
    }
    assert actual == committed, SHAPE_HINT
```

Move the `seeded_state` fixture from `tests/test_routes.py` into
`services/weather/tests/conftest.py` so both modules share it, and remove the
`autouse=True` flag so only tests that request it are affected.

- [ ] **Step 2: Run to confirm the snapshots now fail**

Run: `cd services/weather && uv run pytest tests/test_contract.py -v`
Expected: FAIL — both snapshot assertions, because the surface changed.

- [ ] **Step 3: Regenerate both snapshots**

```bash
cd services/weather
uv run python -c "import json,pathlib; from weather.main import app; pathlib.Path('../../contracts/openapi/weather.json').write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + '\n')"
uv run pytest tests/test_contract.py::test_response_shapes_match_committed_contract -v
```

The second command fails and prints the diff. Regenerate
`contracts/responses/weather.json` from the `actual` dict it reports, formatted
with `indent=2, sort_keys=True` and a trailing newline.

- [ ] **Step 4: Write the platform conformance test**

`tests/test_signal_envelope_conformance.py`:

```python
"""Every collector's emitted envelope conforms to the committed contract.

Platform-level rather than per-service: the point is that the whole fleet
agrees, which no single service's test can assert.
"""

import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "contracts" / "signal-envelope" / "envelope.v1.schema.json").read_text()
)
FIXTURES = sorted(
    (ROOT / "contracts" / "signal-envelope" / "fixtures").glob("*.json")
)


def test_at_least_one_fixture_exists():
    """A conformance suite with no fixtures passes vacuously."""
    assert FIXTURES, "no envelope fixtures found — the gate would pass vacuously"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_fixture_conforms_to_the_envelope_contract(fixture):
    jsonschema.validate(json.loads(fixture.read_text()), SCHEMA)


def test_every_fixture_declares_a_known_collector():
    known = {"weather"}
    for fixture in FIXTURES:
        body = json.loads(fixture.read_text())
        assert body["collector"] in known, fixture.name
```

- [ ] **Step 5: Generate the weather fixtures**

Write two fixtures under `contracts/signal-envelope/fixtures/` —
`weather-venue_forecast_kickoff.json` and `weather-venue_conditions_current.json` —
by capturing the `to_dict()` output from a `capture_week` run against the mocked
upstreams in `tests/test_capture.py`. Also create
`contracts/signal-envelope/collectors/weather.json` describing weather's
field-level shape:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://foundry.internal/signal-envelope/collectors/weather.json",
  "title": "weather collector signal fields",
  "signal_types": {
    "venue_forecast_kickoff": {
      "type": "object",
      "required": [
        "game_id",
        "venue_id",
        "forecast_valid_at",
        "forecast_lead_hours",
        "environment",
        "temperature_f",
        "wind_speed_mph",
        "precipitation_type",
        "playability"
      ],
      "properties": {
        "game_id": { "type": "string" },
        "venue_id": { "type": "string" },
        "forecast_valid_at": { "type": "string", "format": "date-time" },
        "forecast_lead_hours": { "type": "number", "minimum": 0 },
        "environment": {
          "enum": [
            "outdoor",
            "fixed_dome",
            "retractable_open",
            "retractable_closed",
            "retractable_undecided"
          ]
        },
        "temperature_f": { "type": ["number", "null"] },
        "feels_like_f": { "type": ["number", "null"] },
        "wind_speed_mph": { "type": ["number", "null"] },
        "wind_gust_mph": { "type": ["number", "null"] },
        "wind_direction_deg": { "type": ["integer", "null"] },
        "precipitation_type": {
          "enum": ["none", "rain", "snow", "sleet", "freezing_rain", "mixed"]
        },
        "precipitation_probability": { "type": "number", "minimum": 0, "maximum": 1 },
        "precipitation_rate_in_hr": { "type": "number", "minimum": 0 },
        "humidity_pct": { "type": "number", "minimum": 0, "maximum": 100 },
        "bands": { "type": "object" },
        "playability": { "type": "object" }
      }
    },
    "venue_conditions_current": {
      "type": "object",
      "required": ["venue_id", "temperature_f", "wind_speed_mph"],
      "properties": {
        "venue_id": { "type": "string" }
      }
    }
  }
}
```

- [ ] **Step 6: Run both suites to verify they pass**

```bash
cd services/weather && uv run pytest -v
cd ../.. && uv run --with pytest --with jsonschema pytest tests/test_signal_envelope_conformance.py -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add contracts/ tests/ services/weather/tests/
git commit -m "feat(contracts): regenerate weather snapshots, add envelope conformance gate"
```

---

## Task 18: Docker, Helm, MinIO, and local deploy

**Files:**
- Modify: `services/weather/Dockerfile`, `helm/values/weather/values.yaml`, `infra/grafana-stack/helmfile.yaml`, `scripts/deploy-local.py`, `scripts/stack-up.py`
- Create: `infra/grafana-stack/values/minio.yaml`

**Interfaces:**
- Consumes: `LAKE_BUCKET`, `LAKE_ENDPOINT_URL`, AWS credential env vars read by `build_lake_writer_from_env`
- Produces: a deployable image and a local MinIO-backed lake

- [ ] **Step 1: Update the Dockerfile for the workspace member**

`services/weather/Dockerfile` — the build context becomes the repo root, so
paths gain the `services/weather/` prefix:

```dockerfile
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# A uv workspace has ONE lockfile, at the workspace root — there is no
# services/weather/uv.lock (Task 1 deleted it). The root pyproject.toml and
# root uv.lock both come in, plus every member's manifest, because uv resolves
# the whole workspace graph before it can sync any single member.
COPY pyproject.toml uv.lock ./
COPY libs/collector-core/ ./libs/collector-core/
COPY services/weather/pyproject.toml ./services/weather/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --package weather

COPY services/weather/weather/ ./services/weather/weather/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package weather

FROM python:3.12-slim

RUN addgroup --system --gid 65532 app && adduser --system --uid 65532 --ingroup app appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

USER 65532

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "weather.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Verify the image builds from the repo root**

```bash
docker build -f services/weather/Dockerfile -t weather:worktree-test .
```

Expected: build succeeds. Note the `.` context and `-f` flag — this is the
change every future collector inherits.

- [ ] **Step 3: Add MinIO to the local stack**

`infra/grafana-stack/values/minio.yaml`:

```yaml
# Local-only object store standing in for S3. On EKS (Phase 6) the same code
# path talks to real S3 with an IRSA role; only LAKE_ENDPOINT_URL differs.
mode: standalone
replicas: 1
persistence:
  enabled: false          # Kind clusters are disposable; the lake is not
                          # durable locally by design.
resources:
  requests:
    memory: 256Mi
rootUser: foundry
rootPassword: foundry-local-dev
buckets:
  - name: foundry-signals
    policy: none
    purge: false
```

In `infra/grafana-stack/helmfile.yaml`, add the repository and release:

```yaml
repositories:
  # ... existing entries ...
  - name: minio
    url: https://charts.min.io/

releases:
  # ... existing entries ...
  - name: minio
    namespace: monitoring
    chart: minio/minio
    values:
      - values/minio.yaml
```

- [ ] **Step 4: Wire the lake into weather's values**

Append to `helm/values/weather/values.yaml` `extraEnv`:

```yaml
  - name: LAKE_BUCKET
    value: "foundry-signals"
  - name: LAKE_ENDPOINT_URL
    value: "http://minio.monitoring.svc.cluster.local:9000"
  - name: REFRESH_MIN_INTERVAL_SECONDS
    value: "300"
  # Off by default in code so tests and bare uvicorn runs stay hermetic;
  # the deployed collector is the only place the loop actually runs.
  - name: CAPTURE_ENABLED
    value: "true"
  - name: CAPTURE_SEASON
    value: "2026"
  - name: CAPTURE_WEEK
    value: "1"
  # Same optional:true pattern as COLLECTOR_TOKEN — the pod starts before the
  # Secret exists and the lake simply has no credentials until it arrives.
  - name: AWS_ACCESS_KEY_ID
    valueFrom:
      secretKeyRef:
        name: weather-lake-credentials
        key: access-key-id
        optional: true
  - name: AWS_SECRET_ACCESS_KEY
    valueFrom:
      secretKeyRef:
        name: weather-lake-credentials
        key: secret-access-key
        optional: true
  - name: AWS_DEFAULT_REGION
    value: "us-east-1"
```

- [ ] **Step 5: Create the lake credentials Secret locally**

In `scripts/deploy-local.py`, extend the `SERVICES` entry:

```python
SERVICES = {
    "weather": {
        "port": 8000,
        "secret": "weather-collector-token",
        "lake_secret": "weather-lake-credentials",
    },
    # ... player-projections unchanged ...
}
```

Add alongside `ensure_collector_secret`:

```python
# Matches infra/grafana-stack/values/minio.yaml. Kind-only, committed
# deliberately — real credentials are created out of band and never enter Git.
LOCAL_LAKE_ACCESS_KEY = "foundry"
LOCAL_LAKE_SECRET_KEY = "foundry-local-dev"


def ensure_lake_secret(name: str) -> None:
    """Create or update the collector's object-store credentials Secret."""
    print(f"\n$ kubectl create secret generic {name} | kubectl apply -f -")
    run_piped(
        [
            "kubectl", "create", "secret", "generic", name,
            f"--from-literal=access-key-id={LOCAL_LAKE_ACCESS_KEY}",
            f"--from-literal=secret-access-key={LOCAL_LAKE_SECRET_KEY}",
            "--dry-run=client", "-o", "yaml",
        ],
        ["kubectl", "apply", "-f", "-"],
    )
```

and call it next to the existing secret call:

```python
    lake_secret = SERVICES[service].get("lake_secret")
    if lake_secret:
        ensure_lake_secret(lake_secret)
```

Reuse whatever helper `ensure_collector_secret` already uses to pipe the two
commands; do not introduce a second mechanism.

- [ ] **Step 6: Update the docker build invocation**

Any place in `scripts/deploy-local.py` or `scripts/stack-up.py` that runs
`docker build` for a service must now pass the repo root as context:

```python
    run(["docker", "build", "-f", f"services/{service}/Dockerfile",
         "-t", f"{service}:local", "."])
```

- [ ] **Step 7: Teach `build-push` about the build context**

Without this the image build breaks **after merge to main**, not in the PR —
`integration-test` never exercises `build-push`. Add an optional input to
`.github/actions/build-push/action.yml` so existing callers are unaffected:

```yaml
inputs:
  service:
    required: true
    description: Service name, used as the build context path (e.g. weather)
  context:
    required: false
    default: ''
    description: >-
      Docker build context. Defaults to services/<service>. Collectors that
      depend on the libs/collector-core workspace member must pass '.' so the
      library is inside the context.
  dockerfile:
    required: false
    default: ''
    description: Dockerfile path. Defaults to <context>/Dockerfile.
```

and replace the `docker/build-push-action@v6` step's `context`:

```yaml
    - uses: docker/build-push-action@v6
      with:
        context: ${{ inputs.context || format('services/{0}', inputs.service) }}
        file: ${{ inputs.dockerfile || format('services/{0}/Dockerfile', inputs.service) }}
        push: true
        tags: ${{ inputs.image-name }}:${{ inputs.tag }}
        cache-from: type=gha
        cache-to: type=gha,mode=max
```

`player-projections` and `foundry-cli` pass neither input and keep their current
behaviour exactly.

- [ ] **Step 8: Point weather's build at the repo root**

In `.github/workflows/weather.yml`, the `build-push` job's step becomes:

```yaml
      - uses: ./.github/actions/build-push
        with:
          service: weather
          context: .
          dockerfile: services/weather/Dockerfile
          image-name: ghcr.io/kakhavai/foundry/weather
          tag: ${{ github.sha }}
```

- [ ] **Step 8b: Guard against dev-only dependencies imported at runtime**

A real bug this plan already shipped and accidentally un-shipped: `collector-core`'s
`metrics.py` imported `httpx` at module level while `httpx` was declared **dev-only**.
The test suite could never catch it — tests always run with dev dependencies
installed — and the Dockerfile installs with `--no-dev`, so the first symptom
would have been a container crash-looping on `ImportError` in the cluster.

That class of bug is invisible to every test in the repo by construction, so guard
it mechanically. Add to `.github/workflows/weather.yml`, inside the existing
`collector-core-test` job, after the test step:

```yaml
      - name: Every library module must import with runtime deps only
        shell: bash
        working-directory: libs/collector-core
        run: |
          uv sync --frozen --no-dev
          uv run --no-sync python - <<'PY'
          import importlib, pkgutil, sys
          import collector_core
          failed = []
          for m in pkgutil.iter_modules(collector_core.__path__):
              name = f"collector_core.{m.name}"
              try:
                  importlib.import_module(name)
              except Exception as exc:
                  failed.append(f"{name}: {exc!r}")
          if failed:
              print("Modules that fail to import without dev dependencies:")
              for f in failed:
                  print("  ", f)
              sys.exit(1)
          print("all modules import with runtime dependencies only")
          PY
```

This runs in a fresh container so pruning dev dependencies cannot poison a later
step. Verify locally before committing:

```bash
cd libs/collector-core && uv sync --frozen --no-dev &&   uv run --no-sync python -c "import collector_core.routes, collector_core.metrics, collector_core.auth, collector_core.lake, collector_core.cadence, collector_core.refresh, collector_core.envelope, collector_core.coverage; print('OK')"
```

Then restore your dev environment with `uv sync --frozen`.

- [ ] **Step 9: Run the shared library's tests in CI**

Still in `.github/workflows/weather.yml`, add `libs/**` to **both** the
`pull_request` and `push` path filters:

```yaml
      - 'libs/**'
```

and add two jobs reusing the existing composite actions:

```yaml
  collector-core-lint:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-lint
        with:
          working-directory: libs/collector-core

  collector-core-test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/python-test
        with:
          working-directory: libs/collector-core
```

Then gate the image build on them:

```yaml
  build-push:
    needs: [lint, test, helm-lint, collector-core-lint, collector-core-test]
```

The library's tests living under a workflow named for a service is a known
oddity, accepted to avoid a new workflow file. When a second collector consumes
`collector-core`, revisit.

- [ ] **Step 10: Verify the composite action still resolves**

```bash
python -c "import yaml,sys; yaml.safe_load(open('.github/actions/build-push/action.yml')); yaml.safe_load(open('.github/workflows/weather.yml')); print('workflow YAML OK')"
docker build -f services/weather/Dockerfile -t weather:ci-context-test .
```

Expected: YAML parses; the build succeeds from the repo root.

- [ ] **Step 11: Verify end to end on Kind**

```bash
python scripts/stack-up.py
kubectl get pods -n monitoring | grep minio
curl -sf -H "Authorization: Bearer local-dev-token" \
  http://localhost:8000/catalog | python3 -m json.tool
```

Expected: MinIO `Running`; `/catalog` returns the collector description.

- [ ] **Step 12: Commit**

```bash
git add services/weather/Dockerfile helm/ infra/ scripts/ .github/
git commit -m "feat(infra): workspace-aware image build, MinIO lake for the local stack"
```

---

## Task 19: Make `weather` load-testable

**Files:**
- Modify: `libs/collector-core/collector_core/routes.py`, `services/weather/weather/capture.py`, `services/weather/weather/adapters/forecast.py`, `services/weather/weather/adapters/schedule.py`, `docs/architecture/phase-8-data-source-collectors.md`
- Modify tests: `libs/collector-core/tests/test_collector_routes.py`, `services/weather/tests/test_capture.py`

**Interfaces:**
- Consumes: everything already built
- Produces:
  - `/refresh` returns `202` **before** the capture runs, per its own contract
  - `capture_week(..., deadline: datetime | None = None)` — truncates a pass at the deadline, recording the untouched games in `coverage.missing` with reason `deadline_exceeded`
  - `FORECAST_URL` and `SCHEDULE_URL` overridable by environment variable

**Why this is 8A's work.** Phase 5B's load-test PR defers all load coverage of `weather` to 8A, because the old shape made 30 sequential upstream calls per request and a single soak run would have exceeded Open-Meteo's free daily tier many times over. 8A already removed that at the root — the stadium routes are gone and `/signals` serves from memory, so a load test now touches no third party. What remains are three things that would each trip a load test immediately.

**Scope discipline — do NOT touch these.** `tests/load/` and `docs/scale-baselines.md` do not exist on this branch; the Phase 5B PR creates them, and its scripts are `.js` files fed to an in-cluster k6 Job through a ConfigMap by `scripts/run-load.py`, with thresholds calibrated from a first measured run. Writing k6 here would guess wrong on the runner, the file format, and the thresholds, and would land unrunnable files in a directory whose conventions arrive later. Do not create either path. Do not create a `tests/load/weather/` subdirectory — that PR keeps the path flat deliberately, and whoever adds weather's scripts decides the layout once with both services in view.

Likewise **do not edit `docs/architecture/phase-5-resilience-and-ai-testing.md`.** That PR is already editing it to record the deferral. 8A records its side of the obligation in its own phase doc, which that PR does not touch, so the two records point at each other and neither branch fights the other.

### 1. `/refresh` must return before the capture runs

**The defect.** `libs/collector-core/collector_core/routes.py` awaits the full capture before returning:

```python
async with httpx.AsyncClient(timeout=10.0) as client:
    envelopes = await spec.capture(season, week, client=client, lake=spec.lake, now=now)
spec.state.envelopes = envelopes
spec.state.last_capture_at = now
return {"refresh_id": refresh_id, ...}
```

The phase doc contracts the opposite (`docs/architecture/phase-8-data-source-collectors.md`, the `POST /refresh` section): *"Returns `202 Accepted` with a `refresh_id`; the capture runs asynchronously and lands in the lake like any other."* So the route returns 202 — meaning "accepted, working on it" — after already finishing, and the caller blocks for the whole capture. Sixteen games at a 10-second per-call timeout, sequentially, plus the current-conditions pass, is minutes under upstream failure.

- [ ] **Step 1: Write the failing tests**

Add to `libs/collector-core/tests/test_collector_routes.py`:

```python
async def test_refresh_returns_before_the_capture_completes(client, spec):
    """202 means accepted, not finished. A caller must not block on the
    upstream — that is what makes /refresh unusable under load."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_capture(season, week, *, client, lake, now):
        started.set()
        await release.wait()
        return {}

    spec.capture = slow_capture
    response = client.post("/refresh", json={})
    assert response.status_code == 202
    assert response.json()["refresh_id"]
    assert started.is_set(), "the capture should have been dispatched"
    release.set()


async def test_refresh_updates_state_once_the_dispatched_capture_finishes(client, spec):
    """The work still has to land — returning early must not drop it."""
    ...


async def test_a_failed_dispatched_capture_does_not_escape(client, spec, caplog):
    """An upstream failure inside a dispatched capture must be logged, not
    surfaced as an unhandled task exception."""
    ...


async def test_shutdown_cancels_an_in_flight_dispatched_capture(spec):
    """A pending capture must not outlive the app or leak a task."""
    ...
```

Write the bodies out fully. `TestClient` runs the app in its own event loop, so a test that needs to observe an in-flight task will need to drive the lifespan explicitly — use the pattern already in `services/weather/tests/test_routes.py` for anything involving app startup.

- [ ] **Step 2: Dispatch the capture instead of awaiting it**

Restructure the handler so it acquires the gate, dispatches, and returns. Hold a strong reference to every in-flight task — a bare `asyncio.create_task` result can be garbage-collected mid-flight — and discard on completion:

```python
async def _run_capture(spec, season, week, now) -> None:
    """Run a dispatched capture. Never raises: an upstream failure must degrade
    freshness, not surface as an unhandled task exception. `spec.capture`
    already records the failure in its envelope and metrics."""
    try:
        async with spec.client_factory() as client:
            envelopes = await spec.capture(
                season, week, client=client, lake=spec.lake, now=now
            )
        spec.state.envelopes = envelopes
        spec.state.last_capture_at = now
    except Exception:
        logger.exception("dispatched capture failed")
```

and in the route, after the gate check:

```python
        task = asyncio.create_task(_run_capture(spec, season, week, now))
        spec.state.in_flight.add(task)
        task.add_done_callback(spec.state.in_flight.discard)
        return {"refresh_id": refresh_id, "scope": {"season": season, "week": week}}
```

Add `in_flight: set[asyncio.Task] = field(default_factory=set)` to `CaptureState`, and a `cancel_in_flight()` helper that cancels each task and awaits it suppressing `CancelledError`. Call it from weather's `lifespan` shutdown alongside the existing loop cancellation, so a pending refresh cannot outlive the app.

While you are here, add `client_factory: Callable[[], httpx.AsyncClient]` to `CollectorSpec`, defaulting to `lambda: httpx.AsyncClient(timeout=10.0)`. A reviewer flagged that the router currently hardcodes transport configuration for every collector in the fleet, so a collector with a slower or rate-limited upstream cannot override it without forking the router. Defaulting to today's value means no collector's behaviour changes.

### 2. A capture pass needs an aggregate deadline

**The defect.** `capture_week` bounds each upstream call at 10 seconds and the pass as a whole at nothing. The per-game loop is sequential, so total upstream failure costs roughly `games x 10s` for the forecast pass and again for current conditions. In the background loop that overruns the 15-minute cadence and the next tick piles up behind it.

**Enforce it inside `capture_week`, not around it.** A wrapper that cancels the whole coroutine throws away everything captured so far. Checking a deadline between games preserves the partial capture and makes the truncation an explicit, countable fact — which is the same reasoning the coverage block exists for, and consistent with "a failed capture still writes."

- [ ] **Step 3: Write the failing tests**

Add to `services/weather/tests/test_capture.py`:

```python
async def test_a_pass_stops_at_the_deadline_and_records_the_remainder():
    """Truncation is a fact to record, not a silent short week."""
    ...
    assert envelope.coverage.present < envelope.coverage.expected
    assert envelope.coverage.missing  # the games never attempted
    assert any(e["reason"] == "deadline_exceeded" for e in envelope.errors)


async def test_signals_captured_before_the_deadline_are_kept():
    """A partial capture is worth more than none — do not discard it."""
    ...


async def test_no_deadline_captures_everything():
    """Default behaviour is unchanged when no deadline is supplied."""
    ...
```

- [ ] **Step 4: Add the deadline parameter**

Give `capture_week` a `deadline: datetime | None = None` keyword. Before each game's forecast fetch, and before each venue's current-conditions fetch, check whether the deadline has passed; if it has, `acc.fail(key, "deadline_exceeded")` for every remaining key and break out of the loop. Do not raise — the envelope still gets written, with the truncation visible in `coverage.missing` and `errors`.

Thread a deadline through from both callers: the scheduler passes `now + CAPTURE_DEADLINE`, and `/refresh` does the same. Put `CAPTURE_DEADLINE_SECONDS` in the collector spec or read it from env with a sane default (300s) — state which you chose and why in your report.

### 3. The upstream URLs must be overridable

**Why.** A load test needs to point the collector at a fake upstream rather than a third party, and the capture-model tests want the same. The convention is already established two lines below each constant: `_maybe_inject_fault` reads `FAULT_UPSTREAM_*` from the environment.

- [ ] **Step 5: Write the failing tests**

One test per adapter asserting the module honours an override. Note that these constants are read at import, so a test must either reload the module or assert against a helper that reads env at call time — pick one, be consistent across both adapters, and say which in your report. Whichever you choose, the existing `respx` tests must keep working: they mock whatever `FORECAST_URL`/`SCHEDULE_URL` resolve to, so the default must stay byte-identical.

- [ ] **Step 6: Make them env-configurable**

```python
FORECAST_URL = os.getenv("FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
```

```python
SCHEDULE_URL = os.getenv(
    "SCHEDULE_URL",
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
)
```

The defaults must be byte-identical to today's values — the schedule URL in particular will 404 if a character changes, and no unit test would catch it because they all mock the transport. Verify the resolved default equals the current constant exactly.

### 4. Record the inherited obligation

- [ ] **Step 7: Write it down in the phase-8 doc**

Add to `docs/architecture/phase-8-data-source-collectors.md`, in the Testing section, a short subsection:

```markdown
### Load coverage — inherited from Phase 5B

Phase 5B's load-test harness deliberately defers all load coverage of `weather`
to 8A. The reason was structural: the pre-8A service made 30 sequential upstream
calls per request, so a single soak run would have exceeded the upstream's free
daily tier several times over. Load-testing that shape was impossible without
either hammering a third party or building a fake upstream.

8A removes the cause. The stadium routes are gone, `/signals` serves the latest
captured envelope from memory, and no request path calls an upstream — so a load
test against a collector now exercises the collector.

8A discharges the prerequisites and no more:

- `POST /refresh` returns before its capture runs, per its own `202` contract.
  Awaiting the capture made the route unloadtestable and violated the contract.
- A capture pass carries an aggregate deadline, so total upstream failure
  truncates the pass and records it rather than running for `games x timeout`.
- `FORECAST_URL` and `SCHEDULE_URL` are environment-overridable, so a load test
  can point at a fake upstream.

**Still owed, and not 8A's to write:** the k6 scripts themselves. The harness,
the in-cluster runner, the file format, and the thresholds all arrive with Phase
5B's load-test PR, and its `docs/scale-baselines.md` will state that `weather`
is uncovered and why. The follow-up that adds `weather`'s scripts replaces that
statement with measured numbers — it does not simply delete it. Layout for
per-service scripts is decided there, with both services in view, rather than
guessed at here.
```

- [ ] **Step 8: Verify and commit**

```bash
cd libs/collector-core && uv run pytest -v && uv run ruff check . && uv run ruff format --check .
cd ../../services/weather && uv run pytest -v && uv run ruff check . && uv run ruff format --check .
cd ../.. && uv run --with pyyaml==6.0.3 --with pytest==9.0.3 --with jsonschema==4.26.0 pytest tests/ -q && uv lock --check
```

All three suites green. Then:

```bash
git add libs/collector-core services/weather docs/architecture
git commit -m "feat(collector-core): async refresh, capture deadline, configurable upstreams"
```

**Mutation-check before committing.** For each, apply, run both suites, restore:

1. Make `/refresh` await the capture again. A test must fail.
2. Remove the `try/except` inside the dispatched capture so a failure escapes as an unhandled task exception. A test must fail.
3. Ignore the deadline in `capture_week`. A test must fail.
4. Change a URL default by one character. A test must fail — if none does, the default is unverified and a typo would 404 in production.

---

## Task 20: Gateway publishes only the contract paths

**Files:**
- Modify: `helm/charts/generic-service/templates/httproute.yaml`, `helm/charts/generic-service/values.yaml`, `helm/values/weather/values.yaml`, `tests/test_helm_httproute.py`

**Interfaces:**
- Consumes: nothing in Python
- Produces: `gateway.publicPaths` — a list of service-relative path prefixes the gateway is allowed to publish, defaulting to the three contract data routes

**The defect this closes.** The HTTPRoute matches one bare `PathPrefix` (`/collectors/<name>`) and rewrites it to `/`, so **every** route the service serves is reachable from the edge — including the two that are deliberately exempt from bearer auth. Verified against the running app: `/signals` correctly returns 401 without a token, but `/health` returns 200 and `/metrics` returns 200. In-cluster with OTel wired, that second one publishes `collector_capture_failures_total{reason}`, `collector_coverage_ratio`, `collector_staleness_seconds`, and `collector_auth_failures_total` — poll cadence, upstream failure patterns, coverage posture, and a counter that lets someone probing tokens watch their own attempts register. No signal data leaks; operational posture does, to the internet.

The exemption itself is correct and stays. The kubelet's probes and Prometheus's annotation scrape cannot carry a token, and a probe cannot reference a Secret — requiring auth would put the token in plaintext in the Deployment manifest and therefore in the GitOps repo, which is a worse trade than the exposure. But that argument only ever justified exempting those paths **in-cluster**. It says nothing about publishing them at the edge, and conflating the two is the bug.

Arrived with Phase 5B's gateway work rather than with 8A, but 8A defines the contract 25 more collectors inherit, so this is the cheapest moment it will ever be to fix.

**Nothing should break.** `scripts/smoke-test.sh` reaches `/health` and `/metrics` over a `kubectl port-forward` to the Service, not through the gateway. Prometheus scrapes pod annotations directly. Neither path depends on edge routing.

- [ ] **Step 1: Write the failing render assertions**

Add to `tests/test_helm_httproute.py` (follow the existing helpers in that file for rendering the chart — do not invent a new harness):

```python
def test_one_rule_per_public_path(weather_httproute):
    rules = weather_httproute["spec"]["rules"]
    matched = [r["matches"][0]["path"]["value"] for r in rules]
    assert matched == [
        "/collectors/weather/catalog",
        "/collectors/weather/signals",
        "/collectors/weather/refresh",
    ]


def test_each_rule_rewrites_to_the_service_relative_path(weather_httproute):
    """The gateway strips the collector prefix and nothing else, so
    /collectors/weather/signals/convergence still reaches /signals/convergence."""
    for rule in weather_httproute["spec"]["rules"]:
        matched = rule["matches"][0]["path"]["value"]
        rewrite = rule["filters"][0]["urlRewrite"]["path"]["replacePrefixMatch"]
        assert matched == f"/collectors/weather{rewrite}"


def test_health_is_not_published_at_the_edge(weather_httproute):
    """Auth-exempt by necessity in-cluster; that is not a reason to publish it."""
    matched = [
        r["matches"][0]["path"]["value"] for r in weather_httproute["spec"]["rules"]
    ]
    assert not any(m.endswith("/health") for m in matched)


def test_metrics_is_not_published_at_the_edge(weather_httproute):
    """In-cluster this exposes collector_* series — poll cadence, failure
    reasons, coverage, and auth-failure counts. Not an edge surface."""
    matched = [
        r["matches"][0]["path"]["value"] for r in weather_httproute["spec"]["rules"]
    ]
    assert not any(m.endswith("/metrics") for m in matched)


def test_no_rule_matches_the_bare_collector_prefix(weather_httproute):
    """A bare prefix rule would republish everything and silently undo this."""
    matched = [
        r["matches"][0]["path"]["value"] for r in weather_httproute["spec"]["rules"]
    ]
    assert "/collectors/weather" not in matched
    assert "/collectors/weather/" not in matched


def test_empty_public_paths_fails_the_render():
    """Fail loudly rather than rendering a route that publishes nothing, or
    worse, falls back to a bare prefix."""
    with pytest.raises(Exception):
        render_chart(
            values={"gateway": {"enabled": True, "pathPrefix": "/collectors/x",
                                "publicPaths": []}}
        )
```

Adapt the fixture and render-helper names to whatever `tests/test_helm_httproute.py` already uses.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pyyaml==6.0.3 --with pytest==9.0.3 pytest tests/test_helm_httproute.py -v`
Expected: the new assertions fail — the chart currently renders exactly one rule matching the bare prefix.

- [ ] **Step 3: Add the chart default**

In `helm/charts/generic-service/values.yaml`, under `gateway`:

```yaml
gateway:
  # Service-relative path prefixes the gateway may publish. Deliberately NOT
  # the whole service: /health and /metrics are exempt from bearer auth so the
  # kubelet's probes and Prometheus's annotation scrape can reach them, and a
  # probe cannot read a Secret — so requiring a token there would mean
  # committing one in plaintext. Exempting them in-cluster is correct;
  # publishing them at the edge is not, and a bare prefix rule did both.
  publicPaths:
    - /catalog
    - /signals
    - /refresh
```

A collector with extra contract routes adds them here. Weather needs nothing extra — `/signals/convergence` is already covered by the `/signals` prefix.

- [ ] **Step 4: Render one rule per public path**

Rewrite the `rules:` block of `helm/charts/generic-service/templates/httproute.yaml`:

```yaml
{{- if not .Values.gateway.publicPaths }}
{{- fail "gateway.publicPaths must be a non-empty list when gateway.enabled is true" }}
{{- end }}
  rules:
    {{- range $path := .Values.gateway.publicPaths }}
    - matches:
        - path:
            type: PathPrefix
            value: {{ printf "%s%s" $.Values.gateway.pathPrefix $path | quote }}
      filters:
        # Strip the collector prefix and nothing else, so a collector's own
        # sub-routes still resolve: /collectors/weather/signals/convergence
        # reaches /signals/convergence.
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: {{ $path | quote }}
      backendRefs:
        - name: {{ include "generic-service.fullname" $ }}
          port: {{ $.Values.service.port }}
    {{- end }}
```

Note the `$` prefixes inside the `range` — inside the loop, `.` is the path string, so chart values must come off the root context. Getting this wrong renders an empty service name and the route silently points nowhere.

Delete the old comment about the doubled `/collectors/weather/weather/stadiums` path; those routes no longer exist.

- [ ] **Step 5: Run the render assertions**

Run: `uv run --with pyyaml==6.0.3 --with pytest==9.0.3 pytest tests/test_helm_httproute.py -v`
Expected: PASS

- [ ] **Step 6: Lint the chart**

```bash
helm lint helm/charts/generic-service --values helm/values/weather/values.yaml
helm template weather helm/charts/generic-service --values helm/values/weather/values.yaml | grep -A30 "kind: HTTPRoute"
```

Read the rendered output and confirm three rules, correct rewrite targets, and a real backend service name in each.

- [ ] **Step 7: Verify on Kind**

```bash
python scripts/stack-up.py
TOKEN=local-dev-token
GW=http://localhost:8080/collectors/weather
# Contract routes reachable with a token
curl -o /dev/null -sw 'catalog  %{http_code}\n' -H "Authorization: Bearer $TOKEN" "$GW/catalog"
curl -o /dev/null -sw 'signals  %{http_code}\n' -H "Authorization: Bearer $TOKEN" "$GW/signals"
# Exempt paths no longer published at the edge — expect 404 from the gateway
curl -o /dev/null -sw 'health   %{http_code}\n' "$GW/health"
curl -o /dev/null -sw 'metrics  %{http_code}\n' "$GW/metrics"
# Still reachable in-cluster, unchanged
kubectl port-forward svc/weather 8000:8000 &
sleep 3
curl -o /dev/null -sw 'direct health  %{http_code}\n' http://localhost:8000/health
curl -o /dev/null -sw 'direct metrics %{http_code}\n' http://localhost:8000/metrics
```

Expected: `catalog`/`signals` 200, gateway `health`/`metrics` **404**, direct `health`/`metrics` **200**.

- [ ] **Step 8: Commit**

```bash
git add helm/ tests/test_helm_httproute.py
git commit -m "fix(gateway): publish only the contract paths, not the auth-exempt ones"
```

---

## Task 21: Smoke test and documentation

**Files:**
- Modify: `scripts/smoke-test.sh`, `docs/architecture/phase-8-data-source-collectors.md`, `CLAUDE.md`, `docs/onboarding.md`, `README.md`

**Interfaces:**
- Consumes: the deployed service from Task 15
- Produces: a green `integration-test` gate and docs that match the code

- [ ] **Step 1: Rewrite the weather block of `scripts/smoke-test.sh`**

Replace lines 29-65 (the weather section) with:

```bash
TOKEN=local-dev-token
GATEWAY=http://localhost:8080/collectors/weather
AUTH="Authorization: Bearer $TOKEN"

# weather — /health and /metrics are exempt from auth so the kubelet's probes
# and Prometheus's scrape keep working.
curl -sf http://localhost:8000/health | grep '"status":"ok"'
curl -sf http://localhost:8000/metrics | grep '# HELP'

curl -sf -H "$AUTH" http://localhost:8000/catalog | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data['collector'] == 'weather', data
assert set(data['signal_types']) == {
    'venue_forecast_kickoff', 'venue_conditions_current'}, data
print('catalog OK')
"

# The doubled path segment is gone: weather's routes no longer live under
# /weather/, so the gateway's strip of /collectors/weather lands on /signals.
curl -sf -H "$AUTH" "$GATEWAY/signals" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'envelopes' in data, data
print('gateway routing OK')
"

# An unknown signal_type must 422 rather than return an empty list, so a client
# bug surfaces instead of looking like a quiet week.
STATUS=$(curl -o /dev/null -sw '%{http_code}' -H "$AUTH" \
  "$GATEWAY/signals?signal_type=nonsense")
[ "$STATUS" = "422" ] || (echo "unknown signal_type should be 422, got $STATUS" && exit 1)

# Rejections. The second one is the one that matters: it goes straight at the
# Service, bypassing the gateway entirely. Under gateway-only auth it would
# return 200 and this required check would pass over an unprotected path.
STATUS=$(curl -o /dev/null -sw '%{http_code}' "$GATEWAY/signals")
[ "$STATUS" = "401" ] || (echo "gateway without token should be 401, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' http://localhost:8000/signals)
[ "$STATUS" = "401" ] || (echo "direct Service call without token should be 401, got $STATUS" && exit 1)
STATUS=$(curl -o /dev/null -sw '%{http_code}' -H "Authorization: Bearer wrong-token" "$GATEWAY/signals")
[ "$STATUS" = "401" ] || (echo "gateway with wrong token should be 401, got $STATUS" && exit 1)
echo "collector auth OK"
echo "weather: OK"
```

- [ ] **Step 1b: Assert a real lake write from inside the cluster**

Task 18's review left one gap it could not close locally: the union of a real
Pod, `secretKeyRef` credential injection, and cluster-DNS resolution of
`minio.monitoring.svc.cluster.local` was never observed together, because this
machine's Kind cluster is ArgoCD-managed and `deploy-local.py` hits the
documented Server-Side-Apply conflict. CI's `integration-test` job has no ArgoCD
— it runs `deploy-local.py` on a fresh cluster — so an assertion here converts
that CI run into the missing proof.

Add to `scripts/smoke-test.sh`, after the collector-auth block:

```bash
# Force a capture, then prove it reached the object store. This is the only
# check that exercises a real Pod reading its credentials from a Secret and
# resolving MinIO through cluster DNS — the local ArgoCD-managed cluster cannot.
curl -sf -X POST -H "$AUTH" -H 'Content-Type: application/json'   -d '{"season":2026,"week":1}' http://localhost:8000/refresh   | python3 -c "import sys,json; assert json.load(sys.stdin)['refresh_id']; print('refresh accepted')"

# /refresh returns before the capture finishes, so poll rather than assume.
for i in $(seq 1 30); do
  OBJECTS=$(kubectl exec -n monitoring deploy/minio --     sh -c 'ls -R /export/foundry-signals 2>/dev/null | grep -c ".json"' || echo 0)
  [ "$OBJECTS" -gt 0 ] && break
  sleep 2
done
[ "$OBJECTS" -gt 0 ] || (echo "no envelope reached the lake after 60s" && exit 1)
echo "lake write OK ($OBJECTS object(s))"
```

Adapt the `kubectl exec` target to whatever the MinIO chart actually names its
Deployment or StatefulSet — check with `kubectl get all -n monitoring | grep minio`
rather than assuming `deploy/minio`. If the chart uses a StatefulSet, the path
differs.

- [ ] **Step 2: Amend the phase doc where this PR departs from it**

In `docs/architecture/phase-8-data-source-collectors.md`, make three edits:

1. In the `weather` section, change the `coverage.expected` line to:

> **`coverage.expected` means:** one `venue_forecast_kickoff` record per game
> scheduled in the queried week — indoor games included, emitting a
> controlled-environment record rather than being dropped. Games beyond the
> 96-hour horizon are captured at the hourly cadence with a larger
> `forecast_lead_hours` and wider `bands`. Counting only in-horizon games would
> make the coverage denominator move hour by hour, so the ratio could not
> distinguish a healthy collector from one that has captured only the near games.

2. In the `weather` section, change **Depends on** to:

> **Depends on:** a bundled schedule adapter at 8A, supplying `game_id`,
> kickoff timestamps, and per-game roof state. Replaced by `schedule-context`
> at 8B behind the same interface. Reads `venue` for field orientation once it
> lands at 8E.

3. Add to the Staging table's 8A row, after "the `weather` retrofit":

> `weather` ships first within 8A — it is the only 8A collector whose upstream
> already works, so the shared capture library is extracted from a working
> consumer rather than designed against `player-identity`, which is the
> catalog's least representative collector.

- [ ] **Step 3: Document the workspace Dockerfile pattern**

In `CLAUDE.md`, under "Dockerfile Pattern (Canonical)", add before the code block:

```markdown
**Collectors build from the repo root**, not the service directory, because they
depend on the `libs/collector-core/` workspace member by path:

    docker build -f services/<name>/Dockerfile -t <name>:local .

The build stage copies `libs/collector-core/` before `uv sync`, since the lock
cannot resolve without the member present. Services that do not consume the
shared library keep the original service-directory context.
```

Add the same note to `docs/onboarding.md` in the service-creation checklist.

- [ ] **Step 4: Run the full verification sweep**

```bash
cd libs/collector-core && uv run ruff check . && uv run ruff format --check . && uv run pytest -v
cd ../../services/weather && uv run ruff check . && uv run ruff format --check . && uv run pytest -v
cd ../.. && uv run --with pytest --with jsonschema pytest tests/ -v
cd libs/collector-core && uv lock --check
cd ../../services/weather && uv lock --check
```

Expected: all green, both coverage gates met, both locks current.

- [ ] **Step 5: Run the local stack and the smoke test**

```bash
python scripts/stack-up.py
bash scripts/smoke-test.sh
```

Expected: `weather: OK`

- [ ] **Step 6: Commit**

```bash
git add scripts/ docs/ CLAUDE.md README.md
git commit -m "docs(phase-8): amend coverage window and 8A dependencies; smoke test on /signals"
```

- [ ] **Step 7: Run the UAT skill before opening the PR**

Per `CLAUDE.md`, the `superpowers:pr-uat` skill is mandatory before any final PR.
Do not skip it.

---

## Definition of Done

- [ ] `contracts/signal-envelope/` committed with schema, `collectors/weather.json`, and fixtures
- [ ] `libs/collector-core/` a uv workspace member, consumed by `weather` via path dependency, building inside the image
- [ ] `weather` serves `/health`, `/metrics`, `/catalog`, `/signals`, `/signals/convergence`, `/refresh`; stadium routes gone
- [ ] Both signal types emitting, each with its own coverage denominator
- [ ] The capture loop runs on the cadence, escalating into and out of the T−90 min window — proven by test
- [ ] Neutral-site games never resolve to the designated home team's venue — proven by the Munich fixture
- [ ] Bundled table carries `stadium_id` and `roof_type`; every 2026 domestic venue resolves
- [ ] MinIO in the local stack; lake objects written and readable at the documented key layout
- [ ] Both contract snapshots regenerated; `smoke-test.sh` green against the new paths
- [ ] Phase 8 doc amended on the coverage window and 8A dependencies
- [ ] `CLAUDE.md` and `docs/onboarding.md` document the workspace-member build
- [ ] `collector-core` lint+test running in CI; `build-push` builds weather from the repo root
- [ ] CI proves every `collector_core` module imports with runtime dependencies only
- [ ] Gateway publishes only `/catalog`, `/signals`, `/refresh`; `/health` and `/metrics` return 404 at the edge and 200 in-cluster
- [ ] `/refresh` returns before its capture runs; a capture pass carries an aggregate deadline; `FORECAST_URL` and `SCHEDULE_URL` are env-overridable
- [ ] Load-coverage obligation inherited from Phase 5B recorded in the phase-8 doc (NOT the phase-5 doc, and `tests/load/` untouched)
- [ ] `uv lock --check` clean in every changed directory
- [ ] Full suite green, coverage gates met, `integration-test` passing
- [ ] `superpowers:pr-uat` run before the PR opens

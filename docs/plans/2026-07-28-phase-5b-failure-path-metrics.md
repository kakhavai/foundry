# Phase 5B — Failure-Path Metrics Implementation Plan

> **Phase 5B, PR 1 of 4.** Sequence: **failure-path metrics** → collector gateway
> + bearer auth → Chaos Mesh scenarios → k6 load and scale.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both services' upstream failure paths emit Prometheus metrics, so
Phase 5B's chaos scenarios have criteria that can actually fail.

**Architecture:** A new `metrics.py` per service owns its instruments and the
small dicts backing its observable gauges, exposing `record_*` functions that
`main.py` calls at existing failure sites. Instruments are created at import;
the OTel `MeterProvider` is installed by `lifespan` in production and by a
session-scoped pytest fixture in tests. No behavior changes.

**Tech Stack:** Python 3.12, FastAPI, `opentelemetry-api`/`-sdk` 1.42.1,
`opentelemetry-exporter-prometheus` 0.63b1, `prometheus-client`, pytest, uv.

## Global Constraints

- **No new dependencies.** Everything needed is already in both services'
  `pyproject.toml`. `uv lock --check` gates CI, so a new dependency would
  require regenerating and committing the lock in the same PR — avoid entirely.
- **No new top-level directories.** Keeps CI path filters untouched.
- **No behavior changes.** Response bodies, status codes, timeouts, and
  `_state[fmt]["upstream_healthy"]` all stay exactly as they are.
- **No logging.** Metrics only — structured logging is a separate platform-wide
  decision.
- **No new exception classes.** A format mismatch reports `reason="malformed"`.
- **Coverage floor is 80%** (`--cov-fail-under=80`), enforced per service.
- **Line length 88**, ruff `select = ["E", "F", "I"]` — run `ruff format` and
  `ruff check` before every commit.
- **Never weaken a test to make it pass.** Every new test must be demonstrated
  failing before it passes; paste both outputs into the PR.
- **Verify the branch before committing:** `git branch --show-current` must be
  the worktree branch, never `main`.

## Measured baseline

Captured on the worktree branch at `e6fdb70` before any change:

| Service | Tests | Coverage |
|---|---|---|
| `player-projections` | 74 passed | 98.33% |
| `weather` | 40 passed | 100.00% |

## Verified behavior this plan depends on

Each of these was confirmed by running code, not by reading documentation. Do not
re-litigate them; do re-verify if a step's expected output disagrees.

1. OTel appends `_total` to counters and derives `_seconds` from `unit="s"`, so
   `create_counter("upstream_poll_failures")` renders as
   `upstream_poll_failures_total` and
   `create_observable_gauge("upstream_cache_age", unit="s")` renders as
   `upstream_cache_age_seconds`.
2. `PrometheusMetricReader` registers into `prometheus_client`'s default
   `REGISTRY`, which is what `/metrics` already serves. Same scrape path.
3. Instruments created **before** `set_meter_provider` do record afterwards, but
   anything recorded before the provider is installed is **silently lost**. No
   recording happens at import, so this is safe — it is why the test fixture must
   be session-scoped.
4. A labelled series renders as `name{a="1",b="2"} 3.0`; an **unlabelled** series
   renders as `name 3.0`, with no braces. The test helper must parse both.
5. Exception classification order is correct as written below for
   `HTTPStatusError`, `ConnectTimeout`, `ReadTimeout`, `ConnectError`,
   `KeyError`, `ValueError`, and `RuntimeError`. `httpx.TimeoutException`
   subclasses `RequestError`, so it **must** be tested first.
6. Neither service currently has a `conftest.py`.

## Sequencing under stub mode — decided, carry into PRs 3 and 4

Most of the platform is stubbed: `player-projections` returns an empty document
because the generator has not shipped, and there is no frontend. That is a real
constraint on what Phase 5B can honestly prove, and it was weighed against
deferring Phase 5 entirely until the services are real.

**Decision: build now, but do not publish numbers that must be discarded.**

Deferring was rejected because the blocking dependency — the projections
generator — is private, outside this repository, and on no committed timeline, so
deferring Phase 5 behind it defers it indefinitely. `weather` is also genuinely
unstubbed: it calls Open-Meteo for thirty real stadiums with real latency and
real failure modes.

The line is whether a given piece tests **the platform** or **a service under
realistic load**:

- **Payload-independent, build as specified.** `pod-kill`, `resource-pressure`,
  and especially `bad-deploy` — a crash-on-startup image whose rollout Argo CD
  must fail while the previous version stays live. That exercises GitOps,
  probes, and rollback, and does not care what the response body contains.
- **`network-partition` is vacuous as written.** It claims to validate that
  "stub-mode fallback activates." Stub mode is not a fallback; it is the
  permanent state. Cutting the network from an upstream that is never called
  changes nothing observable, so the scenario passes by definition. Drop it or
  rewrite it against a failure the platform can actually exhibit — PR 2's
  gateway and bearer tokens create one (partition the gateway, or revoke a
  token, and assert the failure is visible in metrics).
- **`latency-injection` cannot trip.** See "Deferred" below.
- **k6 baselines must not become a gate yet.** A ramp against
  `{"projections": [], "count": 0}` measures uvicorn, not the service. Real
  documents are ~350 rows / ~45 KB where serialization dominates P95, so every
  number would be invalidated the day the generator ships — and a >20% P95
  regression gate built on them would fire on noise until someone disabled it.
  PR 4 builds the harness and wires CI; `docs/scale-baselines.md` records
  **stub-mode reference numbers, explicitly marked invalid once real documents
  flow**. The regression gate turns on when there is real data.

## Why this comes first

Phase 5B's chaos scenarios each need a pass/fail criterion expressed as a
Prometheus query. A failure mode that emits nothing cannot supply one, so a
scenario written against it passes because nothing is measured — the criterion is
vacuous, and the green result is worse than no result.

Two such failure paths exist today:

**`player-projections`** — `_poll_loop`'s bare `except Exception`
(`main.py:62-63`) sets `upstream_healthy = False` and emits nothing else: no
metric, no exception class, no staleness bound.

**`weather`** — `all_stadiums_weather` (`main.py:44-51`) catches
`HTTPStatusError`, `RequestError`, `KeyError`, `TypeError` and `ValueError` per
stadium and substitutes `weather: None`. The response is HTTP 200 with
`count: 30` whether thirty stadiums resolved or zero did. `smoke-test.sh` asserts
exactly that `count == 30`, so today the merge gate passes with every upstream
call failing.

The second was not in the original Phase 5B scope, but the same argument reaches
it: the `latency-injection` scenario validates "`weather` timeout handling," and
that scenario has no measurable criterion until `weather` emits failure metrics.
Four of five scenarios would otherwise be measurable and one would not.

## Scope

**In:** upstream failure metrics on both services. Observability only.

**Out:** no behavior changes, no new dependencies, no new top-level directories
(therefore no CI path-filter changes), no logging. Structured logging stays a
separate platform-wide decision — nothing forces it yet, and chaos criteria need
metrics rather than prose.

## Decisions

### OTel meter, not `prometheus_client` directly

Both land on the same scrape path: OTel's `PrometheusMetricReader` registers into
`prometheus_client`'s default `REGISTRY`, which is what `/metrics` already
serves. Given that, the OTel meter wins on three counts:

- **Metric names come out as specified for free.** OTel appends `_total` to
  counters and derives `_seconds` from `unit="s"`, producing
  `upstream_poll_failures_total` and `upstream_cache_age_seconds` exactly.
- **Scrape-time gauges are native.** `ObservableGauge` takes a callback and
  supports labels, so cache age is computed when Prometheus scrapes rather than
  when the poll ran. With a 900s poll interval, a value written at poll time
  would be up to fifteen minutes stale. `prometheus_client` cannot do this for a
  *labelled* gauge without a custom collector — `set_function()` does not combine
  with labels.
- **It survives Phase 6.** Moving from a pull reader to OTLP push metrics changes
  the reader, not a line of instrumentation.

An earlier draft argued the opposite on the grounds that a chaos scenario
partitioning the OTel collector would destroy the metrics. That is false and the
reasoning is recorded here so it is not repeated: `PrometheusMetricReader` is a
**pull** reader. Prometheus scrapes the pod directly via annotations and is
unaffected by a broken collector endpoint — only traces are lost. See CLAUDE.md,
"Collector service name."

**Known cost:** recordings made before `set_meter_provider` are silently
dropped — verified, not assumed. This is safe in production because `lifespan`
installs the provider before `_poll_loop` starts, and it is handled in tests by
the session fixture below.

### `upstream_healthy` reports 0 from startup

Including stub mode, which is production today. The upstream genuinely is not
healthy, and a series that always exists is simpler to query than one guarded by
`absent()`. Accepted cost: a red series until the projections generator ships.

`upstream_cache_age_seconds` is the exception — it emits only after a format's
first success, because there is no age to report before one and `0` would read as
"just refreshed," which is the opposite of the truth.

### No new exception classes

`client.py` raises `MalformedSnapshotError` for both a corrupt body
(`client.py:32`) and a format mismatch (`client.py:42`). Splitting those into a
subclass to get a finer `reason` label was considered and rejected: the same
person owns the producer and the consumer, so the exception message already
names which document declared what. Both report `reason="malformed"`. If the
distinction ever needs to be machine-readable, add it then.

## Metrics

### `player-projections`

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `upstream_poll_failures_total` | counter | `format`, `reason` | one increment per failed format per pass |
| `upstream_cache_age_seconds` | observable gauge | `format` | scrape-time; absent until first success |
| `upstream_healthy` | observable gauge | `format` | `0`/`1`; `0` from startup |

### `weather`

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `weather_upstream_requests_total` | counter | — | every upstream attempt |
| `weather_upstream_failures_total` | counter | `reason` | failures only |

Two counters rather than one with an `outcome` label, so the failure ratio is
`failures / requests` without needing to sum across label values.

These close the `count: 30` hole: thirty failed stadium lookups increment
`weather_upstream_failures_total` by thirty, making visible the degradation the
HTTP response deliberately hides. No separate gauge is needed — the counter
carries it.

### `reason` taxonomy

| `reason` | Raised by |
|---|---|
| `http_status` | `httpx.HTTPStatusError` |
| `timeout` | `httpx.TimeoutException` |
| `transport` | `httpx.RequestError` |
| `malformed` | `MalformedSnapshotError`; `KeyError` / `TypeError` / `ValueError` in `weather` |
| `unknown` | anything else |

`httpx.TimeoutException` subclasses `RequestError`, so it **must** be caught
first or every timeout is mislabelled `transport`. The `unknown` bucket exists so
the classifier can never itself raise inside a failure handler — an unclassified
exception must still produce a countable series.

## Implementation

A new `metrics.py` per service. The two are not shared: services in this repo are
independently packaged and there is no common library to put it in.

**`player_projections/metrics.py`** owns its own `_last_success: dict[str, float]`
and `_healthy: dict[str, bool]`, updated through `record_poll_success(fmt)` and
`record_poll_failure(fmt, exc)`. Gauge callbacks read those dicts. It does **not**
import `main`, which is what keeps the import acyclic; `main` seeds `_healthy` to
`False` for each format alongside the existing `_state` initialisation.

`_poll_loop` keeps its single `except Exception` — the loop must survive any
failure, and a stack of `except` clauses would duplicate the classification that
`metrics._reason()` already does in one place. The handler gains a
`record_poll_failure(fmt, exc)` call and the success path moves into an `else:`
block that calls `record_poll_success(fmt)`.
`_state[fmt]["upstream_healthy"]` stays exactly as it is — the API response
contract does not change.

**`weather/metrics.py`** exposes `record_upstream_attempt()` and
`record_upstream_failure(exc)`, called at the existing `except` sites in both
routes. The tuple-catch in `all_stadiums_weather` keeps catching the same
exception types; it gains one call.

## Testing

A session-scoped `conftest.py` fixture installs
`MeterProvider(PrometheusMetricReader())` once per service test run.

This does not violate the existing no-process-state discipline. `test_telemetry`'s
`patched_sdk` fixture already no-ops `metrics.set_meter_provider`
(`test_telemetry.py:57`), so `setup_telemetry` cannot clobber the test provider,
and the OTel guard means the real one never runs under pytest anyway.

Three properties make the tests meaningful rather than decorative:

- **Assert on real scrape output.** Tests read `generate_latest(REGISTRY)`, not
  the SDK's internal view, so they verify precisely what Prometheus will see —
  including OTel's name mangling. A rename that broke a chaos query would fail
  here.
- **Assert on deltas.** A single provider lives for the whole session and
  counters accumulate across tests, so tests read a counter before and after and
  assert the difference.
- **One test per `reason`,** each driving the specific exception, plus a test
  that a format's failure does not touch another format's series — the metric
  analogue of the existing
  `test_one_format_failing_does_not_affect_the_others`.

Every new test must be shown capable of failing: break the guarded code, capture
red, restore, capture green, and paste both into the PR. A test that has never
been observed failing is not evidence.

## What this does not prove

To be carried into `docs/testing-strategy.md` under Known Limits, in that
document's existing tone:

These tests prove the failure paths emit the named series with the right labels
into a Prometheus registry, in-process. They do **not** prove Prometheus is
scraping them in-cluster: nothing here touches scrape configuration, pod
annotations, or the Helm chart. That link is only proven when a chaos scenario
queries these series against a live Prometheus in PR 3 — until then, "the metric
exists" and "the metric is collected" are separate claims and only the first is
tested.

Nor do they establish thresholds. What counts as an unacceptable cache age or
failure rate is a scenario-design question, deliberately left to PR 3.

## Deferred, recorded here so it is not lost

`weather`'s upstream timeout is `10.0s` (`main.py:37`, `main.py:61`), so the
`latency-injection` scenario's "+2s upstream latency" cannot trip it — as
specified, that scenario cannot fail. PR 3 must either inject above the timeout
or revisit the timeout itself. Changing a live request path does not belong in an
observability PR.

## Definition of done

- Both services emit the metrics above, verified against real `/metrics` output
- One test per `reason` per service, each demonstrated failing before passing
- Coverage stays above the 80% floor in both services
- `docs/testing-strategy.md` Known Limits updated with the section above
- Phase 5B doc's failure-path-metrics bullet reflects what shipped, including
  `weather`'s inclusion and the dropped `format_mismatch` reason
- Green: per-service lint / test / helm-lint, `foundry-cli`, `platform-tests`,
  `integration-test`

---

## File Structure

| File | Responsibility |
|---|---|
| `services/player-projections/player_projections/metrics.py` | **Create.** Owns the three instruments, the `_last_success` / `_healthy` dicts behind the gauges, and `_reason()`. Imports `httpx` and `.client`; must **not** import `.main` — that is what keeps the import acyclic. |
| `services/player-projections/player_projections/main.py` | **Modify.** Seed the health gauge next to `_state`; call `record_poll_failure` / `record_poll_success` in `_poll_loop`. |
| `services/player-projections/tests/conftest.py` | **Create.** Session-scoped `MeterProvider`, plus the `metric_value` parsing fixture. |
| `services/player-projections/tests/test_failure_metrics.py` | **Create.** One test per `reason`, gauge behavior, format independence. |
| `services/weather/weather/metrics.py` | **Create.** Two counters and `_reason()`. Imports `httpx` only. |
| `services/weather/weather/main.py` | **Modify.** Record an attempt and any failure at both existing `except` sites. |
| `services/weather/tests/conftest.py` | **Create.** Same harness as player-projections. |
| `services/weather/tests/test_failure_metrics.py` | **Create.** One test per `reason`, plus the `count: 30` blind-spot test. |
| `docs/testing-strategy.md` | **Modify.** Add the "What this does not prove" text to Known Limits. |
| `docs/architecture/phase-5-resilience-and-ai-testing.md` | **Modify.** Update the failure-path-metrics block for what actually shipped. |

The two `metrics.py` files are deliberately not shared. Services here are
independently packaged with their own `pyproject.toml` and lockfile, and there is
no common library to hold shared code; inventing one for ~40 lines would be worse
than the duplication.

---

### Task 1: `player-projections` — test harness and failure counter

**Files:**
- Create: `services/player-projections/tests/conftest.py`
- Create: `services/player-projections/player_projections/metrics.py`
- Create: `services/player-projections/tests/test_failure_metrics.py`
- Modify: `services/player-projections/player_projections/main.py:44-65`

**Interfaces:**
- Consumes: `player_projections.client.MalformedSnapshotError`, `main.FORMATS`,
  `main._poll_loop`, `main._state`, `main._empty_cache`
- Produces:
  - `metrics.record_poll_failure(fmt: str, exc: BaseException) -> None`
  - `metrics.record_poll_success(fmt: str) -> None`
  - `metrics.register_format(fmt: str) -> None`
  - `metrics._reason(exc: BaseException) -> str`
  - `metrics._last_success: dict[str, float]`, `metrics._healthy: dict[str, bool]`
  - pytest fixture `metric_value(name: str, **labels: str) -> float | None`

All work happens in the worktree at
`C:\Users\kakha\Dev\foundry\.claude\worktrees\phase-5b-failure-metrics`. Run
service commands from `services/player-projections`.

- [ ] **Step 1: Create the test harness**

`services/player-projections/tests/conftest.py`:

```python
import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import REGISTRY, generate_latest


@pytest.fixture(scope="session", autouse=True)
def _meter_provider():
    """Install one real MeterProvider for the whole test session.

    Instruments are created at import time and record nothing until a provider
    exists; anything recorded before it is installed is silently lost. Because
    `set_meter_provider` is one-shot per process, this must happen exactly once,
    before any test records. test_telemetry's `patched_sdk` fixture no-ops
    `set_meter_provider`, so `setup_telemetry` cannot clobber this.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )


@pytest.fixture
def metric_value():
    """Read one series out of real /metrics output.

    Asserting on `generate_latest` rather than the SDK's internal view means
    these tests check exactly what Prometheus scrapes, including OTel's name
    mangling — a rename that broke a chaos query fails here.

    Returns None when the series is absent, which is distinct from a series
    present with value 0.0. Counters accumulate for the whole session, so
    callers assert on the delta across an action, not an absolute value.
    """

    def read(name: str, **labels: str) -> float | None:
        for line in generate_latest(REGISTRY).decode().splitlines():
            if line.startswith("#"):
                continue
            head, _, raw_value = line.rpartition(" ")
            if "{" in head:
                series, _, raw_labels = head.partition("{")
                if series != name:
                    continue
                found = {
                    k: v.strip('"')
                    for k, v in (
                        pair.split("=", 1)
                        for pair in raw_labels.rstrip("}").split(",")
                        if pair
                    )
                }
            else:
                if head != name:
                    continue
                found = {}
            if all(found.get(k) == v for k, v in labels.items()):
                return float(raw_value)
        return None

    return read
```

- [ ] **Step 2: Write the failing test**

`services/player-projections/tests/test_failure_metrics.py`:

```python
import asyncio

import httpx
import pytest

from player_projections import main
from player_projections import metrics as pp_metrics
from player_projections.client import MalformedSnapshotError

URL_TEMPLATE = "https://example.test/{format}.json"


@pytest.fixture(autouse=True)
def reset_state():
    """Both the cache and the gauge-backing dicts are module-global."""
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()
    pp_metrics._last_success.clear()
    pp_metrics._healthy.clear()
    for fmt in main.FORMATS:
        pp_metrics.register_format(fmt)
    yield
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()


@pytest.fixture
def one_iteration(monkeypatch):
    """Make the infinite poll loop run exactly one pass, then stop."""

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)


def _always_raise(exc: BaseException):
    async def _fetch(url, expect_format=None):
        raise exc

    return _fetch


def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/ppr.json")
    return httpx.HTTPStatusError(
        "500 Server Error",
        request=request,
        response=httpx.Response(500, request=request),
    )


@pytest.mark.parametrize(
    "make_exc, reason",
    [
        (_http_status_error, "http_status"),
        (lambda: httpx.ConnectTimeout("timed out"), "timeout"),
        (lambda: httpx.ConnectError("connection refused"), "transport"),
        (lambda: MalformedSnapshotError("not a JSON object"), "malformed"),
        (lambda: RuntimeError("something unforeseen"), "unknown"),
    ],
)
async def test_each_failure_class_increments_its_own_reason(
    monkeypatch, one_iteration, metric_value, make_exc, reason
):
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)
    monkeypatch.setattr(main, "fetch_projections", _always_raise(make_exc()))

    before = (
        metric_value("upstream_poll_failures_total", format="ppr", reason=reason)
        or 0.0
    )
    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()
    after = (
        metric_value("upstream_poll_failures_total", format="ppr", reason=reason)
        or 0.0
    )

    assert after - before == 1.0


async def test_a_failing_format_does_not_increment_another_format(
    monkeypatch, one_iteration, metric_value
):
    """The metric analogue of test_one_format_failing_does_not_affect_the_others."""
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)

    async def only_half_ppr_fails(url, expect_format=None):
        if expect_format == "half-ppr":
            raise MalformedSnapshotError("that document is corrupt")
        return []

    monkeypatch.setattr(main, "fetch_projections", only_half_ppr_fails)

    args = {"reason": "malformed"}
    before_bad = (
        metric_value("upstream_poll_failures_total", format="half-ppr", **args) or 0.0
    )
    before_good = (
        metric_value("upstream_poll_failures_total", format="ppr", **args) or 0.0
    )
    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()
    after_bad = (
        metric_value("upstream_poll_failures_total", format="half-ppr", **args) or 0.0
    )
    after_good = (
        metric_value("upstream_poll_failures_total", format="ppr", **args) or 0.0
    )

    assert after_bad - before_bad == 1.0
    assert after_good - before_good == 0.0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov`

Expected: collection error —
`ModuleNotFoundError: No module named 'player_projections.metrics'`.

- [ ] **Step 4: Write the metrics module**

`services/player-projections/player_projections/metrics.py`:

```python
"""Failure-path metrics for the projections poll loop.

Instruments are created at import; the MeterProvider is installed later by
`lifespan` (production) or by the session fixture in conftest (tests). This
module must not import `.main` — `.main` imports this one.
"""

import time

import httpx
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from .client import MalformedSnapshotError

_meter = metrics.get_meter("player_projections")

# OTel appends `_total` to counters and derives `_seconds` from `unit="s"`, so
# these render as `upstream_poll_failures_total` and `upstream_cache_age_seconds`.
_poll_failures = _meter.create_counter(
    "upstream_poll_failures",
    description="Failed upstream projection polls, by scoring format and cause.",
)

# Gauge state. `_healthy` carries every known format from startup so the series
# always exists; `_last_success` stays empty until a format first succeeds,
# because a cache age of 0 would read as "just refreshed" — the opposite of true.
_last_success: dict[str, float] = {}
_healthy: dict[str, bool] = {}


def _reason(exc: BaseException) -> str:
    """Classify a poll failure for the `reason` label.

    Order matters: `httpx.TimeoutException` subclasses `RequestError`, so it must
    be tested first or every timeout is mislabelled `transport`.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "transport"
    if isinstance(exc, MalformedSnapshotError):
        return "malformed"
    return "unknown"


def register_format(fmt: str) -> None:
    """Seed the health gauge so it reports 0 from startup, stub mode included."""
    _healthy.setdefault(fmt, False)


def record_poll_success(fmt: str) -> None:
    _last_success[fmt] = time.time()
    _healthy[fmt] = True


def record_poll_failure(fmt: str, exc: BaseException) -> None:
    _poll_failures.add(1, {"format": fmt, "reason": _reason(exc)})
    _healthy[fmt] = False


def _cache_age_callback(options: CallbackOptions):
    now = time.time()
    for fmt, succeeded_at in _last_success.items():
        yield Observation(now - succeeded_at, {"format": fmt})


def _healthy_callback(options: CallbackOptions):
    for fmt, healthy in _healthy.items():
        yield Observation(1 if healthy else 0, {"format": fmt})


# Observable gauges run their callback at scrape time. That is what makes cache
# age correct with a 900s poll interval — a value written at poll time would be
# up to fifteen minutes stale by the time Prometheus read it.
_meter.create_observable_gauge(
    "upstream_cache_age",
    callbacks=[_cache_age_callback],
    unit="s",
    description="Seconds since this format last polled successfully.",
)
_meter.create_observable_gauge(
    "upstream_healthy",
    callbacks=[_healthy_callback],
    description="1 when the format's last poll succeeded, 0 otherwise.",
)
```

- [ ] **Step 5: Wire the poll loop**

In `services/player-projections/player_projections/main.py`, add to the imports
below `from .client import fetch_projections`:

```python
from . import metrics
```

Immediately after the `_state` assignment (currently line 29), seed the gauge:

```python
# Seed the health gauge so `upstream_healthy` reports 0 from startup rather
# than appearing only after the first poll. Stub mode is production today.
for _fmt in FORMATS:
    metrics.register_format(_fmt)
```

Replace the body of the `for fmt in FORMATS:` loop inside `_poll_loop`:

```python
        for fmt in FORMATS:
            # Each format is tracked independently: one document failing to
            # parse must not mark the other two unhealthy or drop their cache.
            try:
                players = await fetch_projections(
                    _url_for(template, fmt), expect_format=fmt
                )
            except Exception as exc:
                # Deliberately broad — the loop must outlive any single failure.
                # `metrics._reason` does the classification in one place.
                _state[fmt]["upstream_healthy"] = False
                metrics.record_poll_failure(fmt, exc)
            else:
                _state[fmt]["projections"] = players
                _state[fmt]["last_updated"] = _now_iso()
                _state[fmt]["upstream_healthy"] = True
                metrics.record_poll_success(fmt)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov`
Expected: 6 passed (5 parametrized cases + the independence test).

- [ ] **Step 7: Prove the tests can fail**

Temporarily swap the first two checks in `_reason` so `TimeoutException` is
tested after `RequestError`:

```python
    if isinstance(exc, httpx.RequestError):
        return "transport"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
```

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov`
Expected: the `timeout` case FAILS with `assert 0.0 == 1.0`.

**Capture this output for the PR**, then restore the correct order and re-run to
confirm green. A test never observed failing is not evidence.

- [ ] **Step 8: Run the full suite and commit**

```bash
cd services/player-projections
uv run ruff format . && uv run ruff check .
uv run pytest -q
```

Expected: 80 passed (74 baseline + 6 new), coverage still above 80%.

```bash
cd ../..
git branch --show-current   # must NOT be main
git add services/player-projections/
git commit -m "feat(player-projections): count upstream poll failures by cause

_poll_loop's bare except emitted nothing: no metric, no cause, no
staleness bound. Chaos scenarios in PR 3 need a Prometheus query as a
pass/fail criterion, and a failure mode that emits nothing cannot
supply one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `player-projections` — health and staleness gauges

The gauges were defined in Task 1 because they share `metrics.py`. This task
proves their behavior, which is where the subtle requirements live.

**Files:**
- Modify: `services/player-projections/tests/test_failure_metrics.py`

**Interfaces:**
- Consumes: everything Task 1 produced.
- Produces: no new symbols.

- [ ] **Step 1: Write the failing tests**

Append to `services/player-projections/tests/test_failure_metrics.py`:

```python
async def test_healthy_reports_zero_from_startup_in_stub_mode(metric_value):
    """Stub mode is production today. The series must exist and read 0 —
    absent would force every query through absent(), and 1 would be a lie."""
    for fmt in main.FORMATS:
        assert metric_value("upstream_healthy", format=fmt) == 0.0


async def test_cache_age_is_absent_until_the_first_success(metric_value):
    """There is no age before a success. Emitting 0 would read as
    'just refreshed', which is the opposite of the truth."""
    assert metric_value("upstream_cache_age_seconds", format="ppr") is None


async def test_success_sets_healthy_and_starts_the_cache_clock(
    monkeypatch, one_iteration, metric_value
):
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)

    async def ok(url, expect_format=None):
        return [{"id": "p_1", "pos": "WR", "rank": 1}]

    monkeypatch.setattr(main, "fetch_projections", ok)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert metric_value("upstream_healthy", format="ppr") == 1.0
    age = metric_value("upstream_cache_age_seconds", format="ppr")
    assert age is not None
    assert 0.0 <= age < 5.0


async def test_cache_age_is_computed_at_scrape_time_not_poll_time(metric_value):
    """The whole reason for an observable gauge: with a 900s poll interval, a
    value written when the poll ran would be up to fifteen minutes stale."""
    pp_metrics.record_poll_success("ppr")
    pp_metrics._last_success["ppr"] -= 600.0

    age = metric_value("upstream_cache_age_seconds", format="ppr")

    assert age is not None
    assert 600.0 <= age < 605.0


async def test_failure_after_success_flips_healthy_but_keeps_the_age_series(
    monkeypatch, one_iteration, metric_value
):
    """Staleness is the useful signal once an upstream breaks — the age series
    must keep growing rather than disappearing."""
    pp_metrics.record_poll_success("ppr")
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)
    monkeypatch.setattr(
        main, "fetch_projections", _always_raise(httpx.ConnectError("down"))
    )

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert metric_value("upstream_healthy", format="ppr") == 0.0
    assert metric_value("upstream_cache_age_seconds", format="ppr") is not None
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov`
Expected: all pass — Task 1's `metrics.py` already implements this behavior.

If `test_healthy_reports_zero_from_startup_in_stub_mode` fails with `None`, the
`register_format` seeding loop in `main.py` is missing or placed before
`FORMATS`.

- [ ] **Step 3: Prove the scrape-time test can fail**

In `metrics.py`, change `_cache_age_callback` to report the age as of the last
poll rather than now:

```python
def _cache_age_callback(options: CallbackOptions):
    for fmt, succeeded_at in _last_success.items():
        yield Observation(0.0, {"format": fmt})
```

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov`
Expected: `test_cache_age_is_computed_at_scrape_time_not_poll_time` FAILS with
`assert 600.0 <= 0.0`.

**Capture this output for the PR**, then restore the correct callback and re-run.

- [ ] **Step 4: Run the full suite and commit**

```bash
cd services/player-projections
uv run ruff format . && uv run ruff check .
uv run pytest -q
```

Expected: 85 passed, coverage above 80%.

```bash
cd ../..
git branch --show-current   # must NOT be main
git add services/player-projections/tests/test_failure_metrics.py
git commit -m "test(player-projections): pin gauge semantics for health and staleness

Covers the three requirements that are easy to get wrong: health reads 0
from startup including stub mode, cache age is absent until the first
success rather than 0, and age is computed at scrape time so a 900s poll
interval does not make it stale by up to fifteen minutes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `weather` — upstream request and failure counters

**Files:**
- Create: `services/weather/weather/metrics.py`
- Create: `services/weather/tests/conftest.py`
- Create: `services/weather/tests/test_failure_metrics.py`
- Modify: `services/weather/weather/main.py:35-70`

**Interfaces:**
- Consumes: `weather.main.app`, `weather.stadiums.STADIUMS`
- Produces:
  - `metrics.record_upstream_attempt() -> None`
  - `metrics.record_upstream_failure(exc: BaseException) -> None`
  - `metrics._reason(exc: BaseException) -> str`

- [ ] **Step 1: Create the test harness**

Copy `services/player-projections/tests/conftest.py` to
`services/weather/tests/conftest.py` **verbatim**. It contains no
service-specific references. The duplication is deliberate: the services are
independently packaged and share no library.

- [ ] **Step 2: Write the failing tests**

`services/weather/tests/test_failure_metrics.py`:

```python
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from weather.main import app
from weather.stadiums import STADIUMS

client = TestClient(app)

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

_GOOD_BODY = {
    "current": {
        "time": "2026-07-28T12:00",
        "temperature_2m": 18.5,
        "relative_humidity_2m": 65,
        "wind_speed_10m": 12.3,
        "weather_code": 2,
        "precipitation": 0.0,
    }
}


@pytest.mark.parametrize(
    "mock_kwargs, reason",
    [
        ({"return_value": httpx.Response(500)}, "http_status"),
        ({"side_effect": httpx.ConnectTimeout("timed out")}, "timeout"),
        ({"side_effect": httpx.ConnectError("refused")}, "transport"),
        ({"return_value": httpx.Response(200, json={"nope": {}})}, "malformed"),
    ],
)
@respx.mock
def test_each_failure_class_increments_its_own_reason(
    metric_value, mock_kwargs, reason
):
    respx.get(WEATHER_URL).mock(**mock_kwargs)

    before = metric_value("weather_upstream_failures_total", reason=reason) or 0.0
    response = client.get("/weather/stadiums/lambeau")
    after = metric_value("weather_upstream_failures_total", reason=reason) or 0.0

    assert response.status_code == 502
    assert after - before == 1.0


@respx.mock
def test_every_stadium_failure_is_counted_even_though_the_response_is_200(
    metric_value,
):
    """The blind spot this metric exists to close.

    /weather/stadiums swallows per-stadium failures and substitutes None, so it
    returns 200 with count == 30 whether thirty stadiums resolved or zero did —
    and smoke-test.sh asserts exactly that count. Without this counter, total
    upstream failure is indistinguishable from full success.
    """
    respx.get(WEATHER_URL).mock(side_effect=httpx.ConnectError("refused"))

    before = (
        metric_value("weather_upstream_failures_total", reason="transport") or 0.0
    )
    response = client.get("/weather/stadiums")
    after = metric_value("weather_upstream_failures_total", reason="transport") or 0.0

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == len(STADIUMS)
    assert all(s["weather"] is None for s in body["stadiums"])
    assert after - before == float(len(STADIUMS))


@respx.mock
def test_successful_calls_count_as_attempts_but_not_failures(metric_value):
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=_GOOD_BODY))

    attempts_before = metric_value("weather_upstream_requests_total") or 0.0
    failures_before = (
        metric_value("weather_upstream_failures_total", reason="transport") or 0.0
    )
    response = client.get("/weather/stadiums/lambeau")
    attempts_after = metric_value("weather_upstream_requests_total") or 0.0
    failures_after = (
        metric_value("weather_upstream_failures_total", reason="transport") or 0.0
    )

    assert response.status_code == 200
    assert attempts_after - attempts_before == 1.0
    assert failures_after - failures_before == 0.0
```

> **Note on the unlabelled counter:** `weather_upstream_requests_total` renders
> with no braces (`weather_upstream_requests_total 30.0`). The `metric_value`
> fixture handles both forms — do not "fix" it to require braces.

> **Note on test style:** these are **sync** `def` tests, not `async def`,
> matching `tests/test_weather.py`. `TestClient` drives its own event loop, so an
> `async def` test would nest one inside another. `weather`'s pytest config sets
> `asyncio_mode = "auto"`, which makes that mistake easy to introduce silently.

> **Note on `lambeau`:** verified to exist in `weather/stadiums.py` (30 keys:
> `arrowhead`, `highmark`, `lambeau`, `gillette`, `metlife`, …). The test does
> not otherwise depend on which stadium is used.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov` from
`services/weather`.

Expected: collection error — `ModuleNotFoundError: No module named
'weather.metrics'`.

- [ ] **Step 4: Write the metrics module**

`services/weather/weather/metrics.py`:

```python
"""Upstream failure metrics for the Open-Meteo calls.

`/weather/stadiums` deliberately degrades a failed stadium to `weather: None`
and still returns 200 with the full count, so the HTTP response cannot reveal
partial or total upstream failure. These counters can.
"""

import httpx
from opentelemetry import metrics

_meter = metrics.get_meter("weather")

# OTel appends `_total`, so these render as `weather_upstream_requests_total`
# and `weather_upstream_failures_total`. Two counters rather than one with an
# `outcome` label, so the failure ratio is failures/requests without summing
# across label values.
_requests = _meter.create_counter(
    "weather_upstream_requests",
    description="Upstream weather API calls attempted.",
)
_failures = _meter.create_counter(
    "weather_upstream_failures",
    description="Upstream weather API calls that failed, by cause.",
)


def _reason(exc: BaseException) -> str:
    """Classify an upstream failure for the `reason` label.

    Order matters: `httpx.TimeoutException` subclasses `RequestError`, so it
    must be tested first or every timeout is mislabelled `transport`.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "transport"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "malformed"
    return "unknown"


def record_upstream_attempt() -> None:
    _requests.add(1)


def record_upstream_failure(exc: BaseException) -> None:
    _failures.add(1, {"reason": _reason(exc)})
```

- [ ] **Step 5: Wire both routes**

In `services/weather/weather/main.py`, add below
`from .client import fetch_weather_for_coords`:

```python
from . import metrics
```

Replace the body of `all_stadiums_weather`'s stadium loop:

```python
        for stadium in STADIUMS.values():
            metrics.record_upstream_attempt()
            try:
                weather = await fetch_weather_for_coords(
                    stadium["latitude"], stadium["longitude"], client
                )
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                # The response still degrades to None and still reports 30
                # stadiums; the counter is the only place this is visible.
                metrics.record_upstream_failure(exc)
                weather = None
            results.append({**stadium, "weather": weather})
```

Replace the `try` block in `stadium_weather`:

```python
    async with httpx.AsyncClient(timeout=10.0) as client:
        metrics.record_upstream_attempt()
        try:
            weather = await fetch_weather_for_coords(
                stadium["latitude"], stadium["longitude"], client
            )
        except (httpx.HTTPStatusError, KeyError, TypeError, ValueError) as exc:
            metrics.record_upstream_failure(exc)
            raise HTTPException(status_code=502, detail="Weather API error")
        except httpx.RequestError as exc:
            metrics.record_upstream_failure(exc)
            raise HTTPException(status_code=502, detail="Weather API unreachable")
```

Note the existing clause order is already correct here: `httpx.RequestError`
comes second, and `TimeoutException` reaches it, where `_reason` classifies it as
`timeout` rather than `transport`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov`
Expected: 6 passed (4 parametrized + 2).

- [ ] **Step 7: Prove the blind-spot test can fail**

Comment out the `metrics.record_upstream_failure(exc)` call in
`all_stadiums_weather`:

```python
                # metrics.record_upstream_failure(exc)
                weather = None
```

Run: `uv run pytest tests/test_failure_metrics.py -v --no-cov`
Expected: `test_every_stadium_failure_is_counted_even_though_the_response_is_200`
FAILS with `assert 0.0 == 30.0`. Note that the `count == 30` and
`status_code == 200` assertions still pass — which is the point: the HTTP
response is identical whether or not every upstream call failed.

**Capture this output for the PR**, then restore the call and re-run.

- [ ] **Step 8: Run the full suite and commit**

```bash
cd services/weather
uv run ruff format . && uv run ruff check .
uv run pytest -q
```

Expected: 46 passed (40 baseline + 6 new), coverage above 80%.

```bash
cd ../..
git branch --show-current   # must NOT be main
git add services/weather/
git commit -m "feat(weather): count upstream failures hidden by the 200 response

/weather/stadiums degrades a failed stadium to weather: None and still
returns count == 30, which is what smoke-test.sh asserts — so total
upstream failure was indistinguishable from full success. The
latency-injection chaos scenario needs this to have any criterion at all.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Documentation and final verification

**Files:**
- Modify: `docs/testing-strategy.md`
- Modify: `docs/architecture/phase-5-resilience-and-ai-testing.md`

**Interfaces:**
- Consumes: the metric names shipped in Tasks 1–3.
- Produces: nothing code depends on.

- [ ] **Step 1: Add the Known Limits entry**

Append to the "Known Limits of These Tests" section of
`docs/testing-strategy.md`, matching that section's existing tone — each entry
states plainly what a green result does *not* buy:

```markdown
**Failure-path metrics are proven to be emitted, not to be collected.** The
metric tests assert against real `generate_latest()` output, so they prove
`upstream_poll_failures_total`, `upstream_cache_age_seconds`, `upstream_healthy`,
and `weather_upstream_{requests,failures}_total` carry the right labels and
values in-process. Nothing in them touches scrape configuration, pod
annotations, or the Helm chart, so "the metric exists" and "Prometheus is
collecting the metric" remain separate claims and only the first is tested here.
The second is proven when a chaos scenario queries these series against a live
Prometheus in Phase 5B's chaos PR.

They also fix no thresholds. What counts as an unacceptable cache age or failure
rate is a scenario-design question, deliberately left to the chaos work rather
than guessed at here.
```

- [ ] **Step 2: Update the phase doc's metrics block**

In `docs/architecture/phase-5-resilience-and-ai-testing.md`, replace the
"**Failure-path metrics come first.**" paragraph and its code block with:

```markdown
**Failure-path metrics come first — delivered.** `_poll_loop`'s bare
`except Exception` emitted nothing: no metric, no cause, no staleness bound.
That blocked the chaos work, because a scenario's pass/fail criterion is a
Prometheus query and a failure mode that emits nothing cannot supply one.

`weather` had the same defect and was pulled into the same PR: `/weather/stadiums`
swallows per-stadium failures and returns 200 with `count: 30` whether thirty
stadiums resolved or zero did, which is what `smoke-test.sh` asserts. The
`latency-injection` scenario had no measurable criterion without this.

```
upstream_poll_failures_total{format, reason}
upstream_cache_age_seconds{format}
upstream_healthy{format}
weather_upstream_requests_total
weather_upstream_failures_total{reason}
```

`reason` is one of `http_status`, `timeout`, `transport`, `malformed`, or
`unknown`. A format mismatch reports `malformed`: distinguishing it would have
meant a new exception subclass, and the same person owns the producer and the
consumer, so the exception message already carries the detail.

Metrics only, deliberately. Structured logging is a separate platform-wide
decision (plain vs JSON, OTel log bridge or not) that nothing forces yet — no
service logs anything today — and chaos criteria need metrics, not prose.
```

- [ ] **Step 3: Verify docs did not break the platform suite**

```bash
cd "C:/Users/kakha/Dev/foundry/.claude/worktrees/phase-5b-failure-metrics"
uv run pytest tests/ -q
```

Expected: all platform tests pass. They assert on Helm renders and scripts, and
nothing in this PR touches either — a failure here means something unexpected
was modified.

- [ ] **Step 4: Full verification sweep**

Run every gate this PR can break, and record the real exit codes:

```bash
cd services/player-projections && uv run ruff check . && uv run pytest -q; echo "pp exit=$?"
cd ../weather            && uv run ruff check . && uv run pytest -q; echo "weather exit=$?"
cd ../foundry-cli        && uv run pytest -q;                        echo "cli exit=$?"
cd ../..                 && uv run pytest tests/ -q;                 echo "platform exit=$?"
helm lint helm/charts/generic-service -f helm/values/weather/values.yaml
helm lint helm/charts/generic-service -f helm/values/player-projections/values.yaml
```

Expected: every exit code 0.

- [ ] **Step 5: Confirm the metrics appear on a live `/metrics`**

The unit tests read the registry directly. This checks the actual HTTP endpoint,
which is what Prometheus scrapes:

```bash
cd services/player-projections
uv run python -c "
import os
os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'] = ''
from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
metrics.set_meter_provider(MeterProvider(metric_readers=[PrometheusMetricReader()]))
from player_projections.main import app
with TestClient(app) as c:
    body = c.get('/metrics').text
for line in body.splitlines():
    if line.startswith('upstream_'):
        print(line)
"
```

Expected: three `upstream_healthy{format=...} 0.0` lines. No
`upstream_cache_age_seconds` (no poll has succeeded) and no
`upstream_poll_failures_total` (stub mode never polls) — both correct.

- [ ] **Step 6: Commit**

```bash
git branch --show-current   # must NOT be main
git add docs/
git commit -m "docs: record what the failure-path metrics do and do not prove

Adds a Known Limits entry separating 'the metric is emitted' from
'Prometheus is collecting it' — only the first is tested here.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Run UAT before opening the PR**

Required by `CLAUDE.md` and non-negotiable. Invoke the `superpowers:pr-uat`
skill.

Docker Desktop is frequently not running on this machine, and piping
`docker build` to `tail` masks a daemon failure as exit 0. Check the real exit
code, and if the container layer could not be verified, say so plainly in the PR
rather than implying it passed.

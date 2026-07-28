# Phase 5B — Failure-Path Metrics Implementation Plan

> **Phase 5B, PR 1 of 4.** Sequence: **failure-path metrics** → collector gateway
> + bearer auth → Chaos Mesh scenarios → k6 load and scale.

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

`_poll_loop`'s `except Exception` splits into classified handlers calling
`record_poll_failure`, and the success branch calls `record_poll_success`.
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

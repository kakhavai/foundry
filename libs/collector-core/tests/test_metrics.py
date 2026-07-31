import httpx
import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import REGISTRY, generate_latest

from collector_core.metrics import CollectorMetrics, LastValueGauge


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


# --- the series must survive a scrape they were not written before -----------
#
# THE tests this module exists for. A collector records on a capture cadence --
# minutes to hours -- while Prometheus scrapes every 15-30 seconds, so almost
# every scrape happens with no intervening recording. A synchronous OTel gauge
# is absent from all of those, and PromQL cannot tell that apart from a series
# that never existed. Asserting a single scrape passes either way; the second
# consecutive scrape is what distinguishes them.


@pytest.fixture(scope="module")
def scrape():
    """Install one real MeterProvider, then take real Prometheus scrapes.

    Deliberately not autouse and deliberately not session-scoped in a conftest:
    `test_recording_is_inert_without_a_meter_provider` above must keep running
    with no provider installed, and it is defined earlier in this file.
    """
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()])
    )

    def take() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
        series: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        for line in generate_latest(REGISTRY).decode().splitlines():
            if line.startswith("#"):
                continue
            head, _, raw_value = line.rpartition(" ")
            name, _, raw_labels = head.partition("{")
            labels = {
                k: v.strip('"')
                for k, v in (
                    pair.split("=", 1)
                    for pair in raw_labels.rstrip("}").split(",")
                    if pair
                )
            }
            # OTel stamps every point with its instrumentation scope. Those are
            # not part of any collector's contract and would make an
            # exact-label assertion untestable.
            labels = {k: v for k, v in labels.items() if not k.startswith("otel_scope")}
            try:
                series[(name, tuple(sorted(labels.items())))] = float(raw_value)
            except ValueError:
                continue
        return series

    return take


def read(series, name, **labels):
    return series.get((name, tuple(sorted(labels.items()))))


def test_coverage_ratio_is_present_on_a_second_consecutive_scrape(scrape):
    """The defect, stated as a test: one recording, two scrapes, no recording
    in between. A synchronous gauge is absent from the second one."""
    m = CollectorMetrics("survives-coverage")
    m.coverage("venue_forecast_kickoff", 0.42)

    first = scrape()
    second = scrape()
    third = scrape()

    for label, series in (("first", first), ("second", second), ("third", third)):
        assert (
            read(
                series,
                "collector_coverage_ratio",
                collector="survives-coverage",
                signal_type="venue_forecast_kickoff",
            )
            == 0.42
        ), f"collector_coverage_ratio vanished on the {label} scrape"


def test_staleness_is_present_on_a_second_consecutive_scrape(scrape):
    m = CollectorMetrics("survives-staleness")
    m.staleness(12.0)

    first = scrape()
    second = scrape()

    assert (
        read(first, "collector_staleness_seconds", collector="survives-staleness")
        == 12.0
    )
    assert (
        read(second, "collector_staleness_seconds", collector="survives-staleness")
        == 12.0
    ), "collector_staleness_seconds vanished on the second scrape"


def test_every_recorded_signal_type_survives_together(scrape):
    """One label set arriving must not evict the others, and an empty result
    must not pass vacuously — hence the count assertion."""
    m = CollectorMetrics("survives-multi")
    recorded = {"alpha": 0.1, "beta": 0.0, "gamma": 1.0}
    for signal_type, ratio in recorded.items():
        m.coverage(signal_type, ratio)

    scrape()  # discard: the first scrape passes even with the defect present
    series = scrape()

    found = {
        signal_type: read(
            series,
            "collector_coverage_ratio",
            collector="survives-multi",
            signal_type=signal_type,
        )
        for signal_type in recorded
    }
    assert found == recorded


def test_the_counters_are_unaffected(scrape):
    """A counter is a different instrument class and is already cumulative.
    Pinned so a future 'fix' to the counters is a visible change."""
    m = CollectorMetrics("survives-counters")
    m.capture_attempt()
    m.capture_failure(httpx.TimeoutException("x"))
    m.auth_failure("missing")

    scrape()
    series = scrape()

    assert (
        read(series, "collector_capture_requests_total", collector="survives-counters")
        == 1.0
    )
    assert (
        read(
            series,
            "collector_capture_failures_total",
            collector="survives-counters",
            reason="timeout",
        )
        == 1.0
    )
    assert (
        read(
            series,
            "collector_auth_failures_total",
            collector="survives-counters",
            reason="missing",
        )
        == 1.0
    )


def test_the_gauge_still_reports_the_latest_value_and_can_fall(scrape):
    """Observable must not mean sticky-at-the-high-water-mark: a coverage ratio
    that recovers to 0.0 is the single most important thing this series says."""
    m = CollectorMetrics("survives-latest")
    m.coverage("alpha", 0.9)
    m.coverage("alpha", 0.0)

    series = scrape()
    assert (
        read(
            series,
            "collector_coverage_ratio",
            collector="survives-latest",
            signal_type="alpha",
        )
        == 0.0
    )


def test_the_attribute_set_is_exactly_the_documented_one(scrape):
    """Dashboards and chaos criteria join on these labels. An extra or renamed
    one breaks them silently, so the whole set is pinned, not just membership."""
    m = CollectorMetrics("survives-labels")
    m.coverage("alpha", 0.5)
    m.staleness(3.0)

    scrape()
    series = scrape()

    coverage_labels = [
        labels for (name, labels) in series if name == "collector_coverage_ratio"
    ]
    matching = [
        dict(labels)
        for labels in coverage_labels
        if dict(labels).get("collector") == "survives-labels"
    ]
    assert len(matching) == 1
    assert matching[0] == {"collector": "survives-labels", "signal_type": "alpha"}

    staleness_labels = [
        dict(labels)
        for (name, labels) in series
        if name == "collector_staleness_seconds"
        and dict(labels).get("collector") == "survives-labels"
    ]
    assert len(staleness_labels) == 1
    assert staleness_labels[0] == {"collector": "survives-labels"}


def test_last_value_gauge_is_reusable_by_a_per_collector_subclass(scrape):
    """The helper is the fleet's replacement for `meter.create_gauge`, and the
    subclasses in `services/*/*/metrics.py` are its main consumers."""
    gauge = LastValueGauge(
        otel_metrics.get_meter("survives-subclass"),
        "example_subclass_gauge",
        description="A per-collector series.",
    )
    gauge.set(7, {"collector": "survives-subclass"})

    scrape()
    series = scrape()

    assert read(series, "example_subclass_gauge", collector="survives-subclass") == 7.0


def test_upstream_unchanged_is_recorded_as_a_counter(scrape):
    """Named `collector_upstream_unchanged`; OTel appends `_total`. A separate
    series from `collector_capture_failures_total` -- a 304 is a healthy
    outcome, not a failure -- so it is pinned on its own name and label set
    rather than folded into the failure counter's assertions."""
    m = CollectorMetrics("survives-unchanged")
    m.upstream_unchanged()

    scrape()
    series = scrape()

    assert (
        read(
            series,
            "collector_upstream_unchanged_total",
            collector="survives-unchanged",
        )
        == 1.0
    )

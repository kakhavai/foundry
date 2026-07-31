"""The two series that are roster-scope's own.

Asserted against real `/metrics` output rather than the SDK's internal view,
because OTel's name mangling is part of the contract: a Prometheus alert on
`scope_missed_producers_total` breaks if the instrument is renamed to
`scope_missed_producers_count`, and only a scrape-level assertion sees that.
"""

from roster_scope.metrics import COLLECTOR, RosterScopeMetrics, metrics


def test_the_descriptor_gets_a_collector_metrics_instance():
    """`CollectorDescriptor.metrics` is typed `CollectorMetrics`; the subclass
    must stay one so the shared loop's staleness/coverage recording works."""
    from collector_core.metrics import CollectorMetrics

    assert isinstance(metrics, CollectorMetrics)
    assert isinstance(metrics, RosterScopeMetrics)
    assert metrics.collector == "roster-scope" == COLLECTOR


def test_capture_and_the_routes_share_one_instance():
    """Two instances would mean two sets of instruments and half the
    recordings landing on a series nobody queries."""
    from roster_scope.capture import metrics as capture_metrics
    from roster_scope.main import app

    assert capture_metrics is metrics
    assert app.state.collector_spec.metrics is metrics


def test_missed_producers_is_exported_under_its_alertable_name(scrape):
    metrics.missed_producers(0)
    assert scrape()("scope_missed_producers_total", collector=COLLECTOR) is not None, (
        "the series must exist even at zero — PromQL cannot alert on an absent one"
    )


def test_missed_producers_counts_up(metric_value):
    before = metric_value("scope_missed_producers_total", collector=COLLECTOR) or 0.0
    metrics.missed_producers(3)
    after = metric_value("scope_missed_producers_total", collector=COLLECTOR)
    assert after - before == 3.0


def test_the_gauges_survive_a_scrape_they_were_not_written_before(scrape):
    """Prometheus scrapes every 15-30s; this collector captures on a cadence of
    minutes. Almost every scrape therefore happens with no intervening
    recording, and a synchronous OTel gauge is absent from all of them — which
    PromQL cannot tell apart from a series that never existed.

    One scrape passes either way. The second one is the test.
    """
    metrics.stale_depth_charts(4)
    metrics.coverage("scope_membership_weekly", 0.75)

    scrape()  # discard
    second = scrape()

    assert second("roster_scope_stale_depth_charts", collector=COLLECTOR) == 4.0
    assert (
        second(
            "collector_coverage_ratio",
            collector=COLLECTOR,
            signal_type="scope_membership_weekly",
        )
        == 0.75
    )


def test_stale_depth_charts_is_a_gauge_that_can_fall(scrape):
    metrics.stale_depth_charts(5)
    assert scrape()("roster_scope_stale_depth_charts", collector=COLLECTOR) == 5.0
    metrics.stale_depth_charts(0)
    assert scrape()("roster_scope_stale_depth_charts", collector=COLLECTOR) == 0.0


def test_the_shared_fleet_metrics_still_work(scrape):
    metrics.coverage("scope_membership_weekly", 0.5)
    metrics.staleness(12.0)
    read = scrape()
    assert (
        read(
            "collector_coverage_ratio",
            collector=COLLECTOR,
            signal_type="scope_membership_weekly",
        )
        == 0.5
    )
    assert read("collector_staleness_seconds", collector=COLLECTOR) == 12.0


def test_failure_reasons_are_classified(metric_value):
    import httpx

    before = (
        metric_value(
            "collector_capture_failures_total", collector=COLLECTOR, reason="timeout"
        )
        or 0.0
    )
    metrics.capture_failure(httpx.ReadTimeout("slow"))
    after = metric_value(
        "collector_capture_failures_total", collector=COLLECTOR, reason="timeout"
    )
    assert after - before == 1.0

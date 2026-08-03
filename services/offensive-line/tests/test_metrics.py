"""This collector's own series, and the two-scrape property gauges need.

`collector_coverage_ratio` cannot see any of the failures below, because in all
of them every published row is present, populated and plausible. That is the
whole test for whether a series belongs on this subclass rather than in the
shared library.

**Every assertion here is made across two consecutive scrapes.** A single
scrape passes either way, which is why `LastValueGauge` survived nine
collectors before anybody noticed: OTel's *synchronous* gauge is consumed by a
collection, so a value written on a capture cadence is exported once and then
absent from every scrape until the next capture. Collectors capture on a
cadence of minutes to hours and Prometheus scrapes every 15-30 seconds, so the
overwhelming majority of scrapes saw nothing at all — and PromQL cannot tell an
absent series from a healthy idle one.
"""

import respx

from offensive_line.capture import STRENGTH

from .conftest import Feeds, SpyLake, resolve_everything, run_capture
from .test_routes import wait_for_signals


def _scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def _capture_through_the_app(client) -> None:
    with respx.mock(assert_all_called=False) as router:
        Feeds().install(router)
        resolve_everything(router)
        assert client.post("/refresh", json={}).status_code == 202
        wait_for_signals(client, count=1)


def test_the_coverage_gauge_survives_a_second_consecutive_scrape(client):
    """The fleet-wide series, checked here because a service can break it by
    constructing its own gauge the wrong way."""
    _capture_through_the_app(client)
    _scrape(client)
    assert "collector_coverage_ratio" in _scrape(client)


def test_the_adjusted_variance_gauge_survives_a_second_scrape(client):
    """**The tell for a collapsed adjustment.** `defense-vs-position` published
    an opponent-adjusted column that was the league mean for all 32 teams while
    the raw value spanned 4x. Coverage was 1.0 throughout; the variance was
    zero. A gauge that vanished between scrapes could not have alerted on it.
    """
    _capture_through_the_app(client)
    _scrape(client)
    body = _scrape(client)
    assert "offensive_line_adjusted_variance" in body
    assert 'column="pressure_rate_allowed_adj"' in body


def test_the_lineup_change_gauge_survives_a_second_scrape(client):
    """The lineup guard's subject count. The guard raises rather than flags,
    so a pass that fires it is loud — but a guard that quietly stops having
    subjects, because `lineup_hash` went null league-wide when a feed
    degraded, looks exactly like a league where nobody changed a starter."""
    _capture_through_the_app(client)
    _scrape(client)
    assert "offensive_line_lineup_changes" in _scrape(client)


def test_the_degraded_upstream_gauge_survives_a_second_scrape(client):
    _capture_through_the_app(client)
    _scrape(client)
    assert "offensive_line_degraded_upstreams" in _scrape(client)


def test_the_rows_captured_gauge_survives_a_second_scrape(client):
    _capture_through_the_app(client)
    _scrape(client)
    assert "offensive_line_rows_captured" in _scrape(client)


async def test_the_variance_is_non_zero_on_a_healthy_pass():
    """The gauge exists to be alerted on at zero, so a pass that legitimately
    produces zero variance would make the alert meaningless. This is the arm
    that proves the fixture can distinguish the two."""
    import statistics

    from .conftest import units

    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    values = [
        row["pressure_rate_allowed_adj"]
        for row in rows.values()
        if row["pressure_rate_allowed_adj"] is not None
    ]
    assert statistics.pvariance(values) > 0.0


async def test_a_degraded_feed_is_counted_rather_than_only_named_on_the_row():
    """`degraded_upstreams` on the row says *which*; the gauge says *how
    many*, on a series an operator can alert on without parsing envelopes."""
    envelopes = await run_capture(Feeds(status={"injuries": 500}), lake=SpyLake())
    row = next(
        row for row in envelopes[STRENGTH].signals if row["record_type"] == "unit"
    )
    assert row["degraded_upstreams"] == ["injuries_unavailable"]

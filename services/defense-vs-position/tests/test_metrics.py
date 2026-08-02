"""This collector's own Prometheus series, on the surface that serves them.

Two things here are not obvious and both survived several collectors.

**Two consecutive scrapes, not one.** OTel's *synchronous* gauge is last-value
aggregated with the point CONSUMED by a collection: it appears on the first
scrape after a recording and is absent from every scrape after that. A
collector records on a weekly cadence and Prometheus scrapes every 15-30
seconds, so the overwhelming majority of scrapes would see nothing -- and
PromQL cannot tell an absent series from a healthy idle one. A single-scrape
test passes either way, which is exactly why `LastValueGauge` is mandatory.

**A gauge test that asserts a series EXISTS is unfailable.** `LastValueGauge`
is a process global, so once any test in the session has set a value the
series is reported forever. The real bug is a STALE value -- a gauge recorded
on the happy path and skipped on a failure path keeps reporting last week's
number while the collector is broken. So the tests below set a value, run a
pass that should move it, and assert it moved.
"""

import re

import pytest

from . import season
from .conftest import SpyLake, run_capture

SERIES = (
    "defense_vs_position_rows_captured",
    "defense_vs_position_rank_divergences",
    "defense_vs_position_players_resolved_ratio",
)


def scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def value_of(body: str, series: str) -> float:
    match = re.search(rf"^{re.escape(series)}\{{[^}}]*}}\s+([-\d.e+]+)$", body, re.M)
    assert match, f"{series} is absent from /metrics:\n{body[:2000]}"
    return float(match.group(1))


@pytest.mark.parametrize("series", SERIES)
def test_a_series_is_present_on_a_second_consecutive_scrape(client, series):
    """The `meter.create_gauge` regression, caught. A single scrape passes
    with either instrument; the second one is where the synchronous gauge
    disappears."""
    client.post("/refresh", json={})
    scrape(client)
    assert series in scrape(client)


def test_the_rows_gauge_moves_when_the_row_count_does(client, upstreams):
    """Not "the series exists" -- that is unfailable for a process-global
    gauge. Set it, change the world, assert it followed."""
    client.post("/refresh", json={})
    # Poll until the dispatched capture lands, then read the value.
    from .test_routes import wait_for_signals

    wait_for_signals(client, count=1)
    full = value_of(scrape(client), "defense_vs_position_rows_captured")
    assert full == 576.0

    upstreams.set_pbp(season.pbp_document(teams=season.TEAMS[:4]))
    app_spec = client.app.state.collector_spec
    app_spec.refresh_gate._last_allowed_at = None
    client.post("/refresh", json={})

    for _ in range(200):
        if value_of(scrape(client), "defense_vs_position_rows_captured") != full:
            break
        import time

        time.sleep(0.05)
    truncated = value_of(scrape(client), "defense_vs_position_rows_captured")
    assert truncated == 4 * 18, "the gauge kept reporting the previous pass's row count"


async def test_the_resolved_ratio_is_recorded_even_when_nothing_was_asked(upstreams):
    """`seen == 0` records 1.0 rather than dividing.

    A week with no opportunities resolved everything it was asked to;
    recording 0.0 would fire the identity alert on a bye, and recording
    nothing at all would leave the series stale at last week's value.
    """
    from defense_vs_position.metrics import DefenseVsPositionMetrics

    recorded: list[float] = []
    metrics = DefenseVsPositionMetrics()
    metrics._players_resolved_ratio.set = lambda value, attrs: recorded.append(value)
    metrics.players_resolved(0, 0)
    metrics.players_resolved(3, 4)
    assert recorded == [1.0, 0.75]


async def test_a_failed_pass_still_records_the_coverage_gauge(upstreams, monkeypatch):
    """`fail_capture` records `collector_coverage_ratio` at 0.0 rather than
    skipping it. A gauge that stops on an outage reads as a healthy
    collector."""
    recorded: list[tuple[str, float]] = []
    monkeypatch.setattr(
        "collector_core.failure.CollectorMetrics.coverage",
        lambda self, signal_type, ratio: recorded.append((signal_type, ratio)),
    )
    upstreams.set_pbp(b"not a gzip stream at all")
    with pytest.raises(Exception):
        await run_capture(SpyLake())
    assert recorded == [("defense_positional_allowance", 0.0)]

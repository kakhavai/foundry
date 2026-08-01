"""The four collector-owned series, and who records the shared failure counter.

`collector_coverage_ratio` structurally cannot see this collector's own
failure mode: coverage is computed against the same revisions that drove the
fetch, so a *blended* rate and a correct one have identical coverage. That is
what the four gauges below are for.

The other half of this file is the double-count rule. `fail_capture` and
`publish_capture` both record `collector_capture_failures_total`, so a
collector that also records it around them counts one failure twice — and the
degraded and field-level branches are the documented exception, because the
library never sees those at all.
"""

import pytest
from prometheus_client import REGISTRY

from coaching_scheme.metrics import metrics

from .conftest import Feeds, SpyLake, run_capture


def sample(name: str) -> float | None:
    """One gauge's current value, or `None` when the series is absent.

    Absent and zero are different facts and PromQL cannot tell them apart, so
    a test that treated a missing series as 0.0 would pass for a gauge that
    was never recorded at all — which is exactly the bug `LastValueGauge`
    exists to prevent.
    """
    for metric in REGISTRY.collect():
        for metric_sample in metric.samples:
            if metric_sample.name == name:
                return metric_sample.value
    return None


GAUGES = (
    "coaching_scheme_window_straddles",
    "coaching_scheme_unexplained_changepoints",
    "coaching_scheme_play_callers_identified",
    "coaching_scheme_staff_revisions",
)


@pytest.mark.parametrize("gauge", GAUGES)
async def test_every_gauge_is_present_after_a_healthy_pass(gauge, lake: SpyLake):
    """**Including the ones whose value is zero.**

    A gauge only written when it is interesting cannot be alerted on: an
    absent Prometheus series and a healthy idle one are indistinguishable.
    `window_straddles` sitting at 0 is the *expected* state and must still be
    a series.
    """
    await run_capture(lake=lake)
    assert sample(gauge) is not None


async def test_a_quiet_pass_reports_zero_straddles_and_zero_changepoints(
    lake: SpyLake,
):
    await run_capture(lake=lake)
    assert sample("coaching_scheme_window_straddles") == 0
    assert sample("coaching_scheme_unexplained_changepoints") == 0


async def test_an_unexplained_changepoint_moves_its_gauge(lake: SpyLake):
    """The other side. Without it the gauge could be hardcoded to 0 and the
    test above would still pass."""
    from .conftest import proe_with_shift

    await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=20.0)), lake=lake
    )
    assert sample("coaching_scheme_unexplained_changepoints") == 1


async def test_staff_revisions_counts_every_revision_not_every_team(lake: SpyLake):
    """Exactly 32 across the league means one revision per team — on a live
    season, the un-backfilled feed rather than a quiet year. Here: four teams,
    one change, five revisions."""
    from .conftest import coaches_with_change

    await run_capture(lake=lake)
    assert sample("coaching_scheme_staff_revisions") == 4

    await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7)),
        lake=lake,
        now=__import__("datetime").datetime(
            2026, 9, 20, tzinfo=__import__("datetime").UTC
        ),
    )
    assert sample("coaching_scheme_staff_revisions") == 5


async def test_play_callers_identified_tracks_the_curation_backlog(
    lake: SpyLake, monkeypatch
):
    """0 with the register empty, and it must move when an entry is added —
    otherwise it is a constant dressed as a metric."""
    from coaching_scheme import play_callers

    await run_capture(lake=lake)
    assert sample("coaching_scheme_play_callers_identified") == 0

    monkeypatch.setattr(
        play_callers,
        "ASSERTIONS",
        (
            play_callers.PlayCallerAssertion(
                team_id="AAA",
                season=2026,
                play_caller_id="coach-someone",
                play_caller_role="unknown",
                effective_from_week=1,
                asserted_through_week=6,
                source="https://example.test/report",
            ),
        ),
    )
    await run_capture(
        lake=SpyLake(),
        now=__import__("datetime").datetime(
            2026, 9, 21, tzinfo=__import__("datetime").UTC
        ),
    )
    assert sample("coaching_scheme_play_callers_identified") == 1


# --------------------------------------------------------------------------
# The double-count rule
# --------------------------------------------------------------------------


def failure_total() -> float:
    total = 0.0
    for metric in REGISTRY.collect():
        for metric_sample in metric.samples:
            if metric_sample.name == "collector_capture_failures_total":
                total += metric_sample.value
    return total


async def test_a_fatal_upstream_counts_exactly_one_failure(lake: SpyLake):
    """`fail_capture` owns the counter for a failure that ends a pass.
    Calling `metrics.capture_failure(exc)` before it double-counts, and the
    symptom — an alert firing at twice the real rate — reads as a worse
    outage rather than as a bug here."""
    import httpx

    before = failure_total()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(games_status=500), lake=lake)
    assert failure_total() == before + 1


async def test_a_degraded_branch_records_the_counter_itself(lake: SpyLake):
    """The documented exception. Neither `fail_capture` nor a failed
    `publish_capture` write runs when play-by-play is lost but the schedule
    feed is not, so the library cannot see it — and an unrecorded degraded
    pass looks exactly like a healthy one."""
    before = failure_total()
    await run_capture(Feeds(pbp_status=500), lake=lake)
    assert failure_total() == before + 1


async def test_a_field_level_failure_is_also_counted(lake: SpyLake):
    """Two feeds lost, two counts. Losing `personnel_rates` silently would
    make the 46.82 MiB upstream's disappearance invisible."""
    before = failure_total()
    await run_capture(Feeds(ftn_status=500, participation_status=500), lake=lake)
    assert failure_total() == before + 2


async def test_a_failed_lake_write_is_counted_by_the_library(lake: SpyLake):
    """`publish_capture` records it, so the collector must not. An
    object-store outage that left this counter flat is what `injury-report`
    actually shipped."""
    broken = SpyLake(fail_write=True)
    before = failure_total()
    await run_capture(lake=broken)
    # One per signal type: two envelopes, two failed writes.
    assert failure_total() == before + 2


def test_the_metrics_instance_is_a_collector_metrics():
    """`CollectorDescriptor.metrics` takes the shared type, and there must be
    exactly one instance per process — the library never constructs one."""
    from collector_core.metrics import CollectorMetrics

    assert isinstance(metrics, CollectorMetrics)
    assert metrics.collector == "coaching-scheme"

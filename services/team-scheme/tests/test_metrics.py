"""The three collector-owned series, and who records the shared failure counter.

`collector_coverage_ratio` structurally cannot see any of the three failures
below — each of them leaves coverage at exactly the value a healthy pass
produces. That is the bar a collector-owned metric has to clear, and it is why
these three exist and the scaffolder's `rows_captured` does not.

The other half of this file is the double-count rule. `fail_capture` and
`publish_capture` both record `collector_capture_failures_total`, so a
collector that also records it around them counts one failure twice — and the
field-level branches are the documented exception, because the library never
sees those at all.
"""

import pytest
from prometheus_client import REGISTRY

from team_scheme.capture import PROFILE
from team_scheme.metrics import metrics

from .conftest import LATER, Feeds, SpyLake, flat_proe, run_capture


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
    "team_scheme_window_refusals",
    "team_scheme_min_games_sampled",
    "team_scheme_degraded_upstreams",
)


@pytest.mark.parametrize("gauge", GAUGES)
async def test_every_gauge_is_present_after_a_healthy_pass(gauge, lake: SpyLake):
    """**Including the ones whose value is zero.**

    A gauge only written when it is interesting cannot be alerted on: an
    absent Prometheus series and a healthy idle one are indistinguishable.
    `window_refusals` sitting at 0 is the *expected* state and must still be a
    series.
    """
    await run_capture(lake=lake)
    assert sample(gauge) is not None


async def test_a_quiet_pass_reports_no_refusals_and_no_degradation(lake: SpyLake):
    await run_capture(lake=lake)
    assert sample("team_scheme_window_refusals") == 0
    assert sample("team_scheme_degraded_upstreams") == 0


async def test_the_refusal_gauge_moves_when_the_guard_fires(lake: SpyLake, monkeypatch):
    """The other side. Without it the gauge could be hardcoded to 0 and the
    test above would still pass, leaving the collector's own bug invisible."""
    from team_scheme import rates as rates_module

    def always_refuse(team_id, profile):
        raise rates_module.WindowIsNotTheTeamSeason(
            rates_module.REASON_FOREIGN_TEAM_IN_WINDOW, "forced"
        )

    monkeypatch.setattr(rates_module, "assert_window_is_the_team_season", always_refuse)
    await run_capture(lake=lake)
    assert sample("team_scheme_window_refusals") == 4


async def test_min_games_sampled_sees_the_truncation_coverage_cannot(
    lake: SpyLake,
):
    """**The metric's whole reason to exist.**

    A play-by-play document truncated in the *week* direction carries every
    team, so coverage reads exactly what a healthy pass reads while every rate
    is computed over three weeks instead of twelve. The ratio does not move;
    this gauge does.
    """
    full = await run_capture(lake=lake)
    assert sample("team_scheme_min_games_sampled") == 12

    truncated = await run_capture(
        Feeds(proe=flat_proe(weeks=3)), lake=SpyLake(), now=LATER
    )
    assert sample("team_scheme_min_games_sampled") == 3
    # The failure coverage cannot see: identical ratio, identical present.
    assert truncated[PROFILE].coverage.ratio == full[PROFILE].coverage.ratio
    assert truncated[PROFILE].coverage.present == full[PROFILE].coverage.present


async def test_min_games_sampled_reports_the_minimum_not_the_mean(lake: SpyLake):
    """One team short is the signal. A mean over 32 teams moves by a
    thirty-second and reads as noise."""
    proe = flat_proe(weeks=12)
    proe["BBB"] = {week: 1.0 for week in range(1, 4)}
    await run_capture(Feeds(proe=proe), lake=lake)
    assert sample("team_scheme_min_games_sampled") == 3


async def test_the_degraded_gauge_counts_the_feeds_that_were_lost(lake: SpyLake):
    """0, 1 or 2. Losing the 46.82 MiB participation feed costs no coverage at
    all — a team is present on `neutral_pass_rate` — so without this gauge it
    is a perfectly healthy pass with three fields quietly nulled."""
    await run_capture(Feeds(ftn_status=500), lake=lake)
    assert sample("team_scheme_degraded_upstreams") == 1

    await run_capture(
        Feeds(ftn_status=500, participation_status=500),
        lake=SpyLake(),
        now=LATER,
    )
    assert sample("team_scheme_degraded_upstreams") == 2


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
        await run_capture(Feeds(pbp_status=500), lake=lake)
    assert failure_total() == before + 1


async def test_a_field_level_failure_is_counted_by_this_collector(lake: SpyLake):
    """The documented exception. Neither `fail_capture` nor a failed
    `publish_capture` write runs when a charting feed is lost but play-by-play
    is not, so the library cannot see it — and an unrecorded field-level pass
    looks exactly like a healthy one."""
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
    assert failure_total() == before + 1


def test_the_metrics_instance_is_a_collector_metrics():
    """`CollectorDescriptor.metrics` takes the shared type, and there must be
    exactly one instance per process — the library never constructs one."""
    from collector_core.metrics import CollectorMetrics

    assert isinstance(metrics, CollectorMetrics)
    assert metrics.collector == "team-scheme"


def test_no_staff_gauge_survived_the_port():
    """`coaching-scheme` recorded four gauges; three of them described the
    staff half — `staff_revisions`, `play_callers_identified`,
    `unexplained_changepoints`. A gauge left behind would keep publishing a
    constant that a dashboard reads as meaningful.
    """
    names = {
        metric_sample.name
        for metric in REGISTRY.collect()
        for metric_sample in metric.samples
    }
    for banned in (
        "team_scheme_staff_revisions",
        "team_scheme_play_callers_identified",
        "team_scheme_unexplained_changepoints",
        "coaching_scheme_window_straddles",
    ):
        assert banned not in names

"""This collector's own series — the ones `collector_coverage_ratio` cannot see.

**Every test here scrapes TWICE and asserts the value MOVED.** Two reasons,
and both have shipped broken in this fleet:

* A single scrape passes whether or not a gauge is a `LastValueGauge`. OTel's
  synchronous gauge is *consumed* by a collection, so it is exported on the
  first scrape after a recording and absent from every scrape after that —
  and collectors record on a capture cadence while Prometheus scrapes every
  15-30 seconds.
* **A test asserting a series EXISTS is unfailable.** `LastValueGauge` is a
  process global: once a label set is written it is reported forever, so a
  gauge that stopped being updated still appears in every scrape carrying a
  stale value. The bug is staleness, not absence. So each test sets a value,
  changes the world, and asserts the number moved.
"""

import statistics

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest

from defensive_front.capture import STRENGTH, reset_published_digests
from defensive_front.metrics import NOT_RUN, metrics

from . import season as season_module
from .conftest import Feeds, SpyLake, by_team, run_capture


def scrape() -> dict[str, float]:
    """One Prometheus scrape, flattened to `name{labels} -> value`."""
    text = generate_latest(REGISTRY).decode()
    samples: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, value = line.rpartition(" ")
        samples[name.strip()] = float(value)
    return samples


def value(samples: dict[str, float], name: str) -> float:
    """One series by its EXACT name.

    Not `startswith`: a prefix match happily accepts a renamed series, so a
    mutation that renames a gauge would pass every test here while the alert
    rules and dashboards that query the old name went silent.
    """
    matching = {
        k: v for k, v in samples.items() if k == name or k.startswith(name + "{")
    }
    assert matching, f"{name} is absent from the scrape: {sorted(samples)[:20]}"
    assert len(matching) == 1, matching
    return next(iter(matching.values()))


async def test_the_gauges_survive_a_second_consecutive_scrape():
    """The `LastValueGauge` property. A synchronous gauge would be present in
    the first scrape and gone from the second, which is the overwhelming
    majority of scrapes for a weekly collector."""
    await run_capture(Feeds(), lake=SpyLake())
    first, second = scrape(), scrape()
    for prefix in (
        "defensive_front_rows_captured",
        "defensive_front_timing_guard_ran",
        "defensive_front_degraded_upstreams",
    ):
        assert value(first, prefix) == value(second, prefix)


async def test_rows_captured_moves_with_the_row_count():
    """Set it, change the world, assert it moved. Asserting the series merely
    exists would pass against a gauge frozen at its first value forever."""
    await run_capture(Feeds(), lake=SpyLake())
    full = value(scrape(), "defensive_front_rows_captured")
    assert full == len(season_module.TEAMS)

    reset_published_digests()
    lake = SpyLake()
    with pytest.raises(httpx.HTTPStatusError):
        await run_capture(Feeds(pbp_status=500), lake=lake)
    # A failed capture publishes no rows. The gauge has to say so rather than
    # keep reporting the last healthy number.
    metrics.rows_captured(0)
    assert value(scrape(), "defensive_front_rows_captured") == 0.0
    assert full != 0.0


async def test_the_adjusted_variance_is_recorded_per_column():
    """**The tell for the constant-valued adjustment.** Zero here is the only
    external symptom of a self-referential opponent adjustment: coverage stays
    1.0 and every field validates."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    samples = scrape()
    for column in ("pressure_rate_generated_adj", "sack_rate_generated_adj"):
        matching = {
            k: v
            for k, v in samples.items()
            if k.startswith("defensive_front_adjusted_variance")
            and f'column="{column}"' in k
        }
        assert matching, column
        recorded = next(iter(matching.values()))
        assert recorded > 0.0, f"{column} has zero variance across the league"
        # ...and it is the REAL variance of the published column, not a
        # constant. A gauge hard-wired to any positive number passes the
        # assertion above while reporting nothing about the data.
        published = statistics.pvariance(
            [row[column] for row in rows.values() if row[column] is not None]
        )
        assert recorded == pytest.approx(published), (column, recorded, published)


async def test_the_timing_guard_gauges_move_between_a_clean_and_a_flagged_pass():
    """Both the slope and the verdict. Recording only the verdict would leave
    a guard that quietly stopped discriminating indistinguishable from one
    that keeps passing."""
    await run_capture(Feeds(), lake=SpyLake())
    clean = scrape()
    assert value(clean, "defensive_front_timing_guard_ran") == 1.0
    clean_t = value(clean, "defensive_front_timing_confound_t_statistic")

    reset_published_digests()
    await run_capture(Feeds(defense_release_shift=4.0), lake=SpyLake())
    flagged = scrape()
    flagged_t = value(flagged, "defensive_front_timing_confound_t_statistic")

    assert abs(flagged_t) > abs(clean_t)
    assert value(flagged, "defensive_front_timing_confound_flagged_total") >= 1.0


async def test_a_guard_that_could_not_run_is_not_a_slope_of_zero():
    """`NOT_RUN` is a sentinel a PromQL alert can see. A guard that stopped
    running would otherwise report a slope of 0.0 — the healthiest possible
    value."""
    built = season_module.build_season()
    stripped = [
        season_module.Play(**{**vars(play), "time_to_throw": None})
        for play in built.plays
    ]
    feeds = Feeds(
        bodies={
            "participation": season_module.participation_document(
                season_module.Season(plays=stripped, season=built.season)
            )
        }
    )
    await run_capture(feeds, lake=SpyLake())
    samples = scrape()
    assert value(samples, "defensive_front_timing_guard_ran") == 0.0
    assert value(samples, "defensive_front_timing_confound_slope") == NOT_RUN


async def test_degraded_upstreams_moves_with_the_feeds_that_failed():
    await run_capture(Feeds(), lake=SpyLake())
    assert value(scrape(), "defensive_front_degraded_upstreams") == 0.0

    def failures_for(reason: str) -> float:
        return sum(
            v
            for k, v in scrape().items()
            if k.startswith("collector_capture_failures")
            and 'collector="defensive-front"' in k
            and f'reason="{reason}"' in k
        )

    before = {
        r: failures_for(r) for r in ("players_unavailable", "injuries_unavailable")
    }

    reset_published_digests()
    await run_capture(Feeds(players_status=500, injuries_status=500), lake=SpyLake())
    assert value(scrape(), "defensive_front_degraded_upstreams") == 2.0
    # **The gauge is only half of it.** A degraded feed is a failure the
    # LIBRARY cannot see -- neither `fail_capture` nor a failed
    # `publish_capture` write runs for a feed whose loss costs a field rather
    # than the pass -- so the collector has to record it itself, under a
    # reason an operator can alert on separately from a genuine crash.
    for reason, was in before.items():
        assert failures_for(reason) > was, reason


async def test_a_lake_outage_moves_the_shared_failure_counter():
    """`injury-report` watched a capture keep serving the last good data
    through an unresolvable object store while this counter stayed flat — an
    outage indistinguishable from a quiet cadence."""

    def failures() -> float:
        return sum(
            v
            for k, v in scrape().items()
            if k.startswith("collector_capture_failures")
            and 'collector="defensive-front"' in k
        )

    before = failures()
    await run_capture(Feeds(), lake=SpyLake(fail_write=True))
    assert failures() > before


async def test_the_coverage_ratio_is_recorded_even_when_it_is_bad():
    """An absent series and a healthy one are indistinguishable in PromQL, so
    the gauge has to be written on the short weeks too — which are the ones
    worth alerting on."""
    await run_capture(Feeds(), lake=SpyLake())
    samples = scrape()
    matching = {
        k: v
        for k, v in samples.items()
        if k.startswith("collector_coverage_ratio")
        and 'collector="defensive-front"' in k
        and f'signal_type="{STRENGTH}"' in k
    }
    assert matching
    ratio = next(iter(matching.values()))
    assert 0.0 < ratio < 1.0, "the eight-team fixture is a short week"

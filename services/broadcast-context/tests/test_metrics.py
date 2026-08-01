"""This collector's own Prometheus series, checked across TWO scrapes.

A single scrape passes whichever gauge implementation is used, which is why
the synchronous-gauge bug survived nine collectors. OTel's synchronous gauge
is last-value aggregated with the point **consumed** by a collection, so it is
exported on the first scrape after a recording and absent from every scrape
after that. This collector records once a day and Prometheus scrapes every
15-30 seconds, so a synchronous gauge would be missing from essentially every
scrape — and PromQL cannot tell an absent series from a healthy idle one.
"""

from .conftest import NOW, SpyLake, feed_document, run_capture, week_rows

SERIES = (
    "broadcast_context_rows_captured",
    "broadcast_context_unassigned_windows",
    "broadcast_context_incomplete_slate_weeks",
    "broadcast_context_flexed_games",
    "broadcast_context_unevidenced_flex_claims",
)


def _scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def _value(body: str, series: str) -> str:
    line = next(entry for entry in body.splitlines() if entry.startswith(f"{series}{{"))
    return line.rsplit(" ", 1)[-1]


async def test_every_series_survives_a_second_consecutive_scrape(client):
    """The assertion that matters. `LastValueGauge` keeps the series present
    on every scrape; `meter.create_gauge` does not."""
    await run_capture(feed_document(week_rows(1)), lake=SpyLake())

    first, second = _scrape(client), _scrape(client)
    assert len(SERIES) == 5
    for series in SERIES:
        assert series in first, f"{series} missing from the FIRST scrape"
        assert series in second, (
            f"{series} vanished on the SECOND scrape — that is the synchronous "
            "gauge bug; use LastValueGauge"
        )


async def test_the_gauges_carry_a_degraded_pass_s_real_numbers(client):
    """Asserting a series merely exists passes against a gauge that never
    moves, so this pins the numbers a specific degraded pass must produce."""
    rows = [*week_rows(1, drop_kickoff_for=2), *week_rows(2)]
    await run_capture(feed_document(rows), lake=SpyLake())

    body = _scrape(client)
    assert _value(body, "broadcast_context_rows_captured") == "32.0"
    assert _value(body, "broadcast_context_unassigned_windows") == "2.0"
    assert _value(body, "broadcast_context_incomplete_slate_weeks") == "1.0"
    assert _value(body, "broadcast_context_flexed_games") == "0.0"
    assert _value(body, "broadcast_context_unevidenced_flex_claims") == "0.0"


async def test_a_healthy_pass_returns_the_degraded_gauges_to_zero(client):
    """The other arm. Record every pass INCLUDING zero — a gauge written only
    when it is interesting cannot be alerted on, and one that never returns to
    zero reads as a permanent incident."""
    await run_capture(feed_document(week_rows(1)), lake=SpyLake())

    body = _scrape(client)
    assert _value(body, "broadcast_context_rows_captured") == "16.0"
    assert _value(body, "broadcast_context_unassigned_windows") == "0.0"
    assert _value(body, "broadcast_context_incomplete_slate_weeks") == "0.0"


async def test_the_flexed_gauge_moves_when_a_game_is_flexed(client):
    """A gauge that can never fire reads as a passing check. This is the
    collector's actual product, so a season stuck at zero must be
    distinguishable from a broken history read."""
    lake = SpyLake()
    await run_capture(feed_document(week_rows(1)), lake=lake)

    moved = week_rows(1)
    moved[0] = moved[0].replace(",13:00,", ",20:20,")
    await run_capture(feed_document(moved), lake=lake, now=NOW.replace(day=16))

    assert _value(_scrape(client), "broadcast_context_flexed_games") == "1.0"


async def test_the_shared_fleet_series_are_present_too(client):
    """A collector that recorded only its own series would be invisible to the
    fleet-wide dashboards and alerts."""
    await run_capture(feed_document(week_rows(1)), lake=SpyLake())
    body = _scrape(client)
    assert "collector_capture_requests_total" in body
    assert "collector_coverage_ratio" in body
    assert 'collector="broadcast-context"' in body

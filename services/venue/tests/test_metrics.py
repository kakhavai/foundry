"""venue's own series, pinned across TWO consecutive scrapes.

The second scrape is the whole test. OTel's *synchronous* gauge is last-value
aggregated with the point **consumed** by a collection, so it appears on the
first scrape after a recording and is absent from every scrape after that. A
single-scrape test passes either way, which is how that bug survived nine
collectors — and it bites this one hardest, because a `static reference`
cadence records once a day against a 15-second scrape.

`LastValueGauge` is the fix and these tests are what hold it in place.
"""

from venue.capture import ASSIGNMENT, STATIC

from .conftest import SEASON_GAMES, SpyLake, game_row, run_capture, to_csv

SERIES = (
    "venue_rows_captured",
    "venue_revision_window_misses",
    "venue_single_revision_venues",
    "venue_unresolved_venues",
)


def _scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


async def test_every_venue_series_survives_a_second_consecutive_scrape(client):
    """A gauge absent from the second scrape cannot be alerted on, and PromQL
    cannot tell it from a healthy idle series."""
    await run_capture(SpyLake())

    first, second = _scrape(client), _scrape(client)
    assert len(SERIES) == 4
    for series in SERIES:
        assert series in first, f"{series} missing from the FIRST scrape"
        assert series in second, (
            f"{series} vanished on the SECOND scrape — that is the synchronous "
            "gauge bug; use LastValueGauge"
        )


async def test_rows_captured_is_labelled_by_signal_type(client):
    """One unlabelled series would hide an empty signal type behind a full one,
    and this collector's two types come from two different upstreams."""
    await run_capture(SpyLake())
    body = _scrape(client)
    assert f'signal_type="{STATIC}"' in body
    assert f'signal_type="{ASSIGNMENT}"' in body


async def test_the_window_miss_gauge_is_recorded_even_at_zero(client):
    """Record every pass, including zero. A gauge written only when it is
    interesting is a gauge nothing can alert on — and this is the one that
    alerts on the spec's named failure mode."""
    await run_capture(SpyLake())
    body = _scrape(client)
    assert "venue_revision_window_misses" in body
    assert "venue_unresolved_venues" in body


async def test_the_window_miss_gauge_moves_when_a_game_falls_outside(client):
    """A counter that can never fire reads as a passing check.

    Every game here kicks off before the table makes any claim, so every one of
    them is a window miss.
    """
    rows = [
        game_row(week=1, away="CHI", home="GB", gameday="2026-01-11"),
        game_row(week=1, away="MIN", home="DET", gameday="2026-01-11"),
    ]
    await run_capture(SpyLake(), csv=to_csv(rows))

    body = _scrape(client)
    line = next(
        entry
        for entry in body.splitlines()
        if entry.startswith("venue_revision_window_misses{")
    )
    assert line.rsplit(" ", 1)[-1] == "2.0", line


async def test_the_single_revision_gauge_reports_the_full_venue_set(client):
    """The spec's tell, published as a number. Today every venue in the
    committed table has one revision, so this equals the venue count."""
    await run_capture(SpyLake(), csv=to_csv([game_row(week=1, away="CHI", home="GB")]))
    body = _scrape(client)
    line = next(
        entry
        for entry in body.splitlines()
        if entry.startswith("venue_single_revision_venues{")
    )
    assert line.rsplit(" ", 1)[-1] == "1.0", line


async def test_the_shared_fleet_series_are_present_too(client):
    """A collector that recorded only its own series would be invisible to the
    fleet-wide dashboards and alerts."""
    await run_capture(SpyLake())
    body = _scrape(client)
    assert "collector_capture_requests_total" in body
    assert "collector_coverage_ratio" in body
    assert 'collector="venue"' in body
    assert SEASON_GAMES == 272

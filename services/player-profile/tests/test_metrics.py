"""This collector's own series, pinned across TWO consecutive scrapes.

**One scrape passes either way, which is why this survived nine collectors.**
OTel's *synchronous* gauge is last-value aggregated with the point CONSUMED by a
collection: it appears on the first scrape after a recording and is absent from
every scrape after that. `LastValueGauge` wraps `create_observable_gauge` so the
callback runs at collection time and the series stays present.

On a `static reference` cadence the difference is not academic. A pass runs once
a day and Prometheus scrapes every fifteen seconds, so with the wrong instrument
roughly one scrape in 5,760 would carry a value and every alert on it would be
flaky by construction.
"""

import httpx
import respx

from player_profile.capture import capture_player_profile

from .conftest import NOW, SEASON, WEEK, mock_identity, mock_upstreams

OWN_SERIES = (
    "player_profile_rows_captured",
    "player_profile_stale_position_players",
    "player_profile_unresolved_scope_slots",
)


def scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


async def run_capture(lake, **kwargs):
    async with httpx.AsyncClient() as http:
        return await capture_player_profile(
            SEASON, WEEK, client=http, lake=lake, now=NOW, **kwargs
        )


@respx.mock
async def test_every_own_series_survives_a_second_consecutive_scrape(client, lake):
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await run_capture(lake)

    first = scrape(client)
    second = scrape(client)

    for series in OWN_SERIES:
        assert series in first, f"{series} absent from the first scrape"
        assert series in second, (
            f"{series} absent from the SECOND scrape — this is the synchronous-"
            f"gauge bug; use LastValueGauge"
        )


@respx.mock
async def test_the_shared_coverage_gauge_survives_a_second_scrape(client, lake):
    """The library's own gauge, checked here because this collector's digest
    gate records it on a different path for unchanged signal types."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await run_capture(lake)

    scrape(client)
    second = scrape(client)

    assert "collector_coverage_ratio" in second


@respx.mock
async def test_the_gauges_are_recorded_even_when_the_pass_fails_closed(client):
    """An absent Prometheus series and a healthy one are indistinguishable, so
    a gauge that simply stops on a failure path reads as a healthy collector."""
    import pytest
    from collector_core.scope import ScopeUnavailable

    from .conftest import SpyLake

    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    with pytest.raises(ScopeUnavailable):
        await run_capture(SpyLake())

    body = scrape(client)
    for series in OWN_SERIES:
        assert series in body, f"{series} went quiet on the fail-closed path"

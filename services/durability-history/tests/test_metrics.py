"""The collector's own series, pinned across TWO consecutive scrapes.

One scrape passes either way, which is why the `LastValueGauge` bug survived nine
collectors: OTel's *synchronous* gauge is last-value aggregated with the point
**consumed** by a collection, so the series appears on the first scrape after a
recording and is absent from every scrape after that. On a `seasonal` cadence —
one pass a day, a scrape every fifteen seconds — that is roughly one scrape in
5,760 carrying a value, and PromQL cannot tell an absent series from a healthy
idle one.

The two series that earn the subclass are here because `collector_coverage_ratio`
**cannot see this collector's named failure mode at all**: a player whose
suspension games were folded into `career_games_missed_injury` produces a
complete, well-formed, fully-covered record.
"""

import httpx
import respx

from durability_history.capture import capture_durability_history

from .conftest import NOW, SEASON, WEEK, mock_identity, mock_upstreams

SERIES = (
    "durability_history_rows_captured",
    "durability_history_undesignated_absences",
    "durability_history_attribution_violations",
    "durability_history_unresolved_scope_slots",
)


async def capture(lake):
    async with httpx.AsyncClient() as client:
        return await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW
        )


@respx.mock
async def test_every_series_survives_a_second_consecutive_scrape(client, lake):
    """The `LastValueGauge` guarantee. A single scrape passes either way."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)

    first = client.get("/metrics").text
    second = client.get("/metrics").text

    for series in SERIES:
        assert series in first, f"{series} missing from the FIRST scrape"
        assert series in second, (
            f"{series} missing from the SECOND scrape — this is the synchronous "
            "gauge bug; use LastValueGauge"
        )


@respx.mock
async def test_the_undesignated_gauge_counts_the_absences_we_refused_to_attribute(
    client, lake
):
    """Charlie's week-4 absence has no designation, so it is not an injury — and
    burying it would make a quiet injury report indistinguishable from a healthy
    league. It is the number that moves first if the feed stops publishing."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)

    body = client.get("/metrics").text
    line = next(
        line
        for line in body.splitlines()
        if line.startswith("durability_history_undesignated_absences{")
    )
    assert line.rsplit(" ", 1)[1] == "1.0", line


@respx.mock
async def test_the_violation_gauge_is_recorded_at_ZERO_on_a_healthy_pass(client, lake):
    """Record every pass, including zero. A gauge written only when it is
    interesting cannot distinguish "the assertion held" from "this collector
    stopped running", and those are the two states an operator has to tell
    apart."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)

    body = client.get("/metrics").text
    line = next(
        line
        for line in body.splitlines()
        if line.startswith("durability_history_attribution_violations{")
    )
    assert line.rsplit(" ", 1)[1] == "0.0", line


def _value(body: str, series: str) -> str:
    line = next(line for line in body.splitlines() if line.startswith(f"{series}{{"))
    return line.rsplit(" ", 1)[1]


@respx.mock
async def test_a_failed_pass_RESETS_the_gauges_rather_than_leaving_them_stale(
    client, lake
):
    """Seeding is not about the series *existing* — `LastValueGauge` reports a
    label set forever once written, so by the time any later test runs they all
    exist regardless. That is exactly why an existence assertion is not a test.

    What seeding actually buys is that a FAILED pass does not leave the previous
    pass's numbers standing. Without it, a collector that captured one
    undesignated absence and then lost its upstream keeps reporting `1` forever,
    and an operator reads a stale value as a current one.
    """
    from durability_history.adapters import upstream as upstream_mod

    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)
    assert _value(client.get("/metrics").text, SERIES[1]) == "1.0", (
        "the fixture no longer produces an undesignated absence"
    )

    respx.mock.get(upstream_mod.PLAYERS_URL).respond(503)
    respx.mock.get(upstream_mod.GAMES_URL).respond(503)
    try:
        await capture(lake)
    except Exception:  # noqa: BLE001 — the failure is the point
        pass

    body = client.get("/metrics").text
    for series in SERIES:
        assert series in body, f"{series} absent after a failed pass"
    assert _value(body, SERIES[1]) == "0.0", (
        "a failed pass left the previous pass's count standing — the gauges are "
        "not seeded before the first thing that can fail"
    )


@respx.mock
async def test_the_fleet_wide_coverage_gauge_is_recorded_per_signal_type(client, lake):
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)

    body = client.get("/metrics").text
    assert "collector_coverage_ratio" in body
    for signal_type in (
        "player_durability_profile",
        "player_injury_history",
        "player_return_trajectory",
    ):
        assert signal_type in body, signal_type

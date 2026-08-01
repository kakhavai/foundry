"""officiating's own series, read back off a real `/metrics` scrape.

These are the series `collector_coverage_ratio` cannot see, because coverage
counts rows published and every failure they watch for publishes a row that
looks perfectly well-formed: a refused rate, an assignment describing the wrong
people, a crosswalk joining the wrong games.

Two things this file does deliberately.

**It asserts VALUES, not the existence of a series.** A gauge label set
persists once written, so a test that checks `"officiating_rates_refused" in
body` passes for ever after the first test in the session that recorded
anything at all — including a later pass that recorded the wrong number.

**It scrapes twice.** OTel's *synchronous* gauge is last-value aggregated with
the point **consumed** by a collection, so it appears on the first scrape after
a recording and is absent from every scrape after that. A single-scrape test
passes either way. `LastValueGauge` is the fix and the second scrape is what
holds it in place.
"""

import re

import httpx
import respx

from officiating.capture import ASSIGNMENT, capture_officiating

from .conftest import (
    DEFAULT_PENALTIES,
    NOW,
    SEASON,
    SpyLake,
    mock_upstreams,
    officials_csv,
    season_of,
)

SERIES = (
    "officiating_rates_refused",
    "officiating_low_continuity_assignments",
    "officiating_referee_disagreements",
    "officiating_crews_sampled",
    "officiating_undersized_crews",
)

SUBSTITUTE_POSITIONS = (
    "Umpire",
    "Down Judge",
    "Line Judge",
    "Field Judge",
    "Side Judge",
    "Back Judge",
)


def _scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.text


def value(body: str, series: str) -> float:
    """One gauge's value out of a Prometheus exposition body.

    Matched by prefix rather than by an exact label set: the OTel exporter
    attaches `otel_scope_*` labels alongside this collector's own `collector`
    label, and pinning all of them would make the test fail on an SDK upgrade
    that is nothing to do with the collector.
    """
    match = re.search(rf"^{series}\{{[^}}]*\}} ([0-9.e+-]+)$", body, re.MULTILINE)
    assert match, f"{series} is absent from the scrape"
    return float(match.group(1))


async def run_capture(games=None, **overrides):
    mock_upstreams(games if games is not None else season_of(), **overrides)
    async with httpx.AsyncClient() as client:
        return await capture_officiating(
            SEASON, 1, client=client, lake=SpyLake(), now=NOW
        )


def _churn(games, referee_id: str, week: int):
    for game in games:
        if game.referee_id == referee_id and game.week == week:
            game.members = tuple(
                (f"9{index}{referee_id}", f"Substitute {index}", position)
                for index, position in enumerate(SUBSTITUTE_POSITIONS)
            )
            return game
    raise AssertionError("no such game in the fixture")


@respx.mock
async def test_every_series_survives_a_second_consecutive_scrape(client):
    """A gauge absent from the second scrape cannot be alerted on, and PromQL
    cannot tell it from a healthy idle series. This collector is on a `weekly`
    cadence against a 15-second scrape, so all but one scrape in 5,760 is a
    "second" one."""
    await run_capture()

    first, second = _scrape(client), _scrape(client)

    assert len(SERIES) == 5
    for series in SERIES:
        assert series in first, f"{series} missing from the FIRST scrape"
        assert series in second, (
            f"{series} vanished on the SECOND scrape — that is the synchronous "
            "gauge bug; use LastValueGauge"
        )


@respx.mock
async def test_a_healthy_pass_records_the_crew_count_and_a_quiet_alarm(client):
    """Zero is a value, not an absence. A gauge only written when it is
    interesting cannot be alerted on."""
    await run_capture(season_of(crews=8, weeks=6))
    body = _scrape(client)

    assert value(body, "officiating_crews_sampled") == 8.0
    assert value(body, "officiating_low_continuity_assignments") == 0.0
    assert value(body, "officiating_referee_disagreements") == 0.0


@respx.mock
async def test_a_rate_that_is_constant_across_crews_is_refused(client):
    """Half of this fixture's rates are refused, and the number says which.

    `crew_penalties` varies only the False Start count, so `dpi_per_game`,
    `dpi_yards_per_game`, `offensive_holding_per_game` and
    `defensive_holding_per_game` are identical for every crew in every week:
    no between-crew variance, no measurable correlation, nothing to shrink.
    Four rates x eight crews = 32, and asserting 32 rather than "> 0" is what
    distinguishes this from every rate being refused.
    """
    await run_capture(season_of(crews=8, weeks=6))
    body = _scrape(client)

    assert value(body, "officiating_rates_refused") == 32.0


@respx.mock
async def test_a_league_with_no_variation_at_all_refuses_every_rate(client):
    """Eight rates x eight crews. The number is what separates this from the
    test above — a gauge stuck at either value fails one of them."""
    games = season_of(crews=8, weeks=6)
    for game in games:
        game.penalties = DEFAULT_PENALTIES
        game.offensive_plays = 120

    await run_capture(games)
    body = _scrape(client)

    assert value(body, "officiating_rates_refused") == 64.0


@respx.mock
async def test_the_continuity_alarm_moves_the_gauge(client):
    """The spec's separate alarm, made observable. Without this the guard
    exists only in an `errors` array nobody scrapes."""
    games = season_of(crews=8, weeks=6)
    _churn(games, "703", week=6)

    await run_capture(games)
    body = _scrape(client)

    assert value(body, "officiating_low_continuity_assignments") == 1.0


@respx.mock
async def test_a_nickname_does_not_move_the_disagreement_gauge(client):
    """The real 2025 finding: `games.csv` says "Ron Torbert" where
    `officials.csv` says "Ronald Torbert", on all 17 of that crew's games.
    Compared strictly that is a phantom eighteenth crew and a gauge screaming
    about a crosswalk that is working perfectly."""
    games = season_of(crews=8, weeks=6)
    for game in games:
        game.referee_name = "Ronald Torbert"
        game.schedule_referee = "Ron Torbert"

    await run_capture(games)
    body = _scrape(client)

    assert value(body, "officiating_referee_disagreements") == 0.0


@respx.mock
async def test_a_genuine_referee_conflict_does_move_the_gauge(client):
    """What the loose comparison must still catch: a crosswalk that has started
    joining the wrong games together."""
    games = season_of(crews=8, weeks=6)
    for game in games:
        if game.week == 1:
            game.schedule_referee = "Carl Cheffers"

    await run_capture(games)
    body = _scrape(client)

    assert value(body, "officiating_referee_disagreements") == 8.0


@respx.mock
async def test_a_full_crew_leaves_the_undersized_gauge_at_zero(client):
    """Zero is a value. The fixture's crews are all seven-strong, which is what
    every season except 2022 and 2024 looks like."""
    await run_capture(season_of(crews=8, weeks=6))
    body = _scrape(client)

    assert value(body, "officiating_undersized_crews") == 0.0


@respx.mock
async def test_a_short_crew_is_counted_and_published_rather_than_dropped(client):
    """The guard `undersized` exists for, wired end to end.

    Not hypothetical: measured across the live feed for every season 2015-2025,
    2022 ships one six-person crew and **2024 ships eleven**. The position
    vocabulary has already churned once in this file's history ("Head
    Linesman" became "Down Judge" in 2017), so a feed that starts omitting a
    position is a thing that happens.

    Both halves are asserted. The count moves, AND the assignment is still
    published — dropping it would lose a real game, and silently accepting it
    would mix a six-term continuity mean with seven-term ones and leave nothing
    to explain the drift.
    """
    games = season_of(crews=8, weeks=6)
    short = [g for g in games if g.referee_id == "702" and g.week == 3][0]
    short.positions = (
        "Umpire",
        "Down Judge",
        "Line Judge",
        "Field Judge",
        "Side Judge",
    )

    envelopes = await run_capture(games)
    body = _scrape(client)

    assert value(body, "officiating_undersized_crews") == 1.0

    rows = {row["game_id"]: row for row in envelopes[ASSIGNMENT].signals}
    assert short.game_id in rows, "the assignment must still be published"
    assert len(rows[short.game_id]["crew_members"]) == 6
    reasons = [e["reason"] for e in envelopes[ASSIGNMENT].errors]
    assert "undersized_crew" in reasons, reasons
    assert envelopes[ASSIGNMENT].coverage.present == 48, "counted as captured"


@respx.mock
async def test_the_undersized_gauge_survives_the_error_cap(client):
    """The gauge is the reliable channel; the envelope entry is best-effort.

    `undersized` goes onto the envelope through `add_error`, so it queues
    behind the routine `crew_not_published` entries and the 50-entry cap drops
    it in the state this collector spends most of a season in. The gauge is
    written outside the array and is exact regardless.

    Both halves are asserted, because the docstring in `crews.py` now claims
    exactly this and a claim without a test is how the last overclaim got in:
    the envelope entry is **gone**, and the count is still **right**.
    """
    schedule = season_of(crews=8, weeks=12)
    played = [g for g in schedule if g.week <= 2]
    short = [g for g in played if g.referee_id == "702" and g.week == 2][0]
    short.positions = (
        "Umpire",
        "Down Judge",
        "Line Judge",
        "Field Judge",
        "Side Judge",
    )

    mock_upstreams(
        schedule, officials_response=httpx.Response(200, text=officials_csv(played))
    )
    async with httpx.AsyncClient() as http:
        envelopes = await capture_officiating(
            SEASON, 1, client=http, lake=SpyLake(), now=NOW
        )
    body = _scrape(client)

    errors = envelopes[ASSIGNMENT].errors
    assert any(e["reason"] == "errors_truncated" for e in errors), (
        "the fixture must actually overflow the cap, or this proves nothing"
    )
    assert not any(e["reason"] == "undersized_crew" for e in errors), (
        "if this starts passing, the entry now survives the cap and the "
        "docstring in crews.py should be upgraded to claim it"
    )
    assert value(body, "officiating_undersized_crews") == 1.0
    # And the assignment is published regardless of which channel reported it.
    rows = {row["game_id"]: row for row in envelopes[ASSIGNMENT].signals}
    assert len(rows[short.game_id]["crew_members"]) == 6


@respx.mock
async def test_the_degraded_pass_still_records_every_gauge(client):
    """A gauge that went quiet on the degraded path would read exactly like a
    collector that had stopped — so they are pinned at zero rather than left at
    whatever the previous pass wrote.

    The failure counter is the other half: the degraded path is the documented
    case a COLLECTOR counts for itself, because neither `fail_capture` nor
    `publish_capture` runs on it.
    """
    games = season_of(crews=8, weeks=6)
    _churn(games, "703", week=6)
    before = _failures(_scrape(client))

    await run_capture(games, pbp_response=httpx.Response(404))
    body = _scrape(client)

    assert value(body, "officiating_rates_refused") == 0.0
    assert value(body, "officiating_crews_sampled") == 0.0
    assert value(body, "officiating_low_continuity_assignments") == 0.0
    assert _failures(body) == before + 1.0


def _failures(body: str) -> float:
    match = re.search(
        r'^collector_capture_failures_total\{[^}]*reason="penalties_unavailable"'
        r"[^}]*\} ([0-9.e+-]+)$",
        body,
        re.MULTILINE,
    )
    return float(match.group(1)) if match else 0.0

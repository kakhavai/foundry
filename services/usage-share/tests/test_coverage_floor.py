"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These tests
are the ones that fail if somebody "simplifies" `capture.py` by computing the
expectation from the document it just streamed.
"""

from datetime import UTC, datetime, timedelta

import httpx

from usage_share.adapters.upstream import TeamDenominators, UsageRow, WeekUsage
from usage_share.capture import (
    EXPECTED_FLOOR,
    LEAGUE_TEAMS,
    OFFENSIVE_SCOPE_SLOTS_PER_TEAM,
    SIGNAL_TYPES,
    capture_usage_share,
)

from .conftest import (
    SAMPLE_PLAYER_ROWS,
    SAMPLE_TEAMS,
    NOW,
    SpyLake,
    full_league_csv,
    to_csv,
)

FLOOR = EXPECTED_FLOOR["player_usage_weekly"]


async def _capture(lake=None):
    async with httpx.AsyncClient() as client:
        return await capture_usage_share(
            2026, 1, client=client, lake=lake or SpyLake(), now=NOW
        )


def test_the_floor_is_derived_from_the_declared_universe():
    """Spelled out rather than assumed: 32 teams x (11 offensive-skill scope
    slots + 1 denominators object). Not a count of anything a fetch returned."""
    assert LEAGUE_TEAMS == 32
    assert OFFENSIVE_SCOPE_SLOTS_PER_TEAM == 11
    assert FLOOR == LEAGUE_TEAMS * (OFFENSIVE_SCOPE_SLOTS_PER_TEAM + 1) == 384


async def test_a_truncated_upstream_does_not_report_full_coverage(upstream):
    """The failure this floor exists for: an upstream returning ten keys of
    hundreds must not yield `expected: 10, present: 10`, ratio 1.0."""
    envelopes = await _capture()
    envelope = envelopes["player_usage_weekly"]

    observed = SAMPLE_PLAYER_ROWS + SAMPLE_TEAMS
    assert envelope.coverage.expected == FLOOR
    assert envelope.coverage.present == observed
    assert envelope.coverage.ratio == observed / FLOOR
    assert envelope.coverage.ratio < 0.1
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_an_empty_upstream_reports_zero_not_one(serve_upstream):
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing."""
    serve_upstream(to_csv([]))
    envelopes = await _capture()
    for envelope in envelopes.values():
        assert envelope.coverage.expected == FLOOR
        assert envelope.coverage.present == 0
        assert envelope.coverage.ratio == 0.0


async def test_expansion_past_the_floor_still_reports_honestly(serve_upstream):
    """The floor must not CAP a genuine count, only raise a short one.

    This collector routinely observes more than 384 keys — it captures every
    offensive-skill player the feed carries, not only the ~352 in roster-scope's
    watchlist — so a floor that capped would understate reality every week.
    """
    serve_upstream(full_league_csv(players_per_team=20))
    envelopes = await _capture()
    envelope = envelopes["player_usage_weekly"]

    observed = LEAGUE_TEAMS * (20 + 1)
    assert observed > FLOOR
    assert envelope.coverage.expected == observed
    assert envelope.coverage.present == observed
    assert envelope.coverage.ratio == 1.0


async def test_a_partial_league_is_floored_not_shrunk(serve_upstream):
    """Half the league reporting must read as half, not as complete."""
    serve_upstream(full_league_csv(teams=16))
    envelopes = await _capture()
    envelope = envelopes["player_usage_weekly"]

    assert envelope.coverage.expected == FLOOR
    assert envelope.coverage.present == 16 * 12
    assert envelope.coverage.ratio == 0.5


async def test_a_stream_cut_short_by_the_deadline_reports_zero_not_complete(upstream):
    """Over budget before a single row was retained. A truncated pass that
    reports itself truncated is useful; one that reports itself complete is the
    silent hole the coverage block exists to catch.

    The deadline is wall-clock — deliberately not derived from `NOW`, which is
    a frozen instant next month and would never expire.
    """
    expired = datetime.now(tz=UTC) - timedelta(hours=1)
    async with httpx.AsyncClient() as client:
        envelopes = await capture_usage_share(
            2026, 1, client=client, lake=SpyLake(), now=NOW, deadline=expired
        )
    envelope = envelopes["player_usage_weekly"]

    assert envelope.signals == []
    assert envelope.coverage.present == 0
    assert envelope.coverage.ratio == 0.0
    reasons = {error["reason"] for error in envelope.errors}
    assert "deadline_exceeded" in reasons, envelope.errors


async def test_rows_left_unbuilt_by_the_deadline_are_missing_not_dropped(monkeypatch):
    """The other half of the deadline path: the fetch finished inside budget
    but the mapping ran out of it. Every row still owed must land in
    `coverage.missing` rather than shrinking the numerator and the denominator
    together, which would read as a smaller but perfectly healthy week.
    """
    usage = WeekUsage(
        rows=[
            UsageRow(
                upstream_player_id=f"00-KC-{i:02d}",
                game_id="2026_01_BUF_KC",
                team="KC",
                position="WR",
                targets=1,
                air_yards=10.0,
                carries=0,
                upstream_target_share=0.5,
            )
            for i in range(2)
        ],
        denominators={
            "KC": TeamDenominators(
                team="KC", dropbacks=30, targets=2, air_yards=20.0, carries=0,
                upstream_target_share_sum=1.0,
            )
        },
    )

    async def already_fetched(*args, **kwargs):
        return usage

    monkeypatch.setattr("usage_share.capture.fetch_week_usage", already_fetched)
    expired = datetime.now(tz=UTC) - timedelta(hours=1)
    async with httpx.AsyncClient() as client:
        envelopes = await capture_usage_share(
            2026, 1, client=client, lake=SpyLake(), now=NOW, deadline=expired
        )
    envelope = envelopes["player_usage_weekly"]

    assert envelope.signals == []
    # The team's denominators still resolved — they are accounted for before
    # the row loop — so this is 1 of 3, not 0 of 3.
    assert envelope.coverage.present == 1
    assert sorted(envelope.coverage.missing) == ["player:00-KC-00", "player:00-KC-01"]
    reasons = {error["reason"] for error in envelope.errors}
    assert "deadline_exceeded" in reasons, envelope.errors


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert EXPECTED_FLOOR, "no floors declared — the check below would be vacuous"
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())

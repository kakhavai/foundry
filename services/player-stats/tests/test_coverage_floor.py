"""Coverage accounting — the thing most likely to be got wrong, and silently.

`coverage.expected` must never derive from what a fetch returned. These are the
tests that fail if somebody "simplifies" `capture.py` by computing the
expectation from `rows`, and the ones that hold the spec's definition:

    One row per roster-scope watchlist player whose team has completed its game
    for the scoped week, plus any non-watchlist player who recorded at least
    one offensive snap in those games.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx
from collector_core.lake import lake_key
from collector_core.scope import ScopeUnavailable

from player_stats.adapters.upstream import stats_url
from player_stats.capture import (
    BOX_SIGNAL,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    TEAM_COUNT,
    WATCHLIST_SLOTS_PER_TEAM,
    capture_player_stats,
)

from .conftest import (
    NOW,
    SEASON,
    WEEK,
    SpyLake,
    feed_csv,
    feed_row,
    scope_envelope,
    seed_scope,
    stub_player_id,
)


async def capture(rows, lake=None, *, watchlist=None, **kwargs):
    """A capture over `rows`, with a roster-scope watchlist seeded into the
    lake. `watchlist=None` seeds the team-defense-anchor-only default (see
    `conftest.seed_scope`) — an empty player watchlist, successfully
    fetched, matching the old unscoped baseline most of these tests describe.
    """
    lake = SpyLake() if lake is None else lake
    seed_scope(lake, watchlist or ())
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(200, text=feed_csv(rows))
        )
        async with httpx.AsyncClient() as client:
            return await capture_player_stats(
                SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
            )


# ── the floor itself ──────────────────────────────────────────────────────────


def test_the_floor_is_the_config_universe_minus_the_team_defenses():
    """32 teams x (2 QB + 3 RB + 4 WR + 2 TE + 1 K) = 384. roster-scope's own
    universe is 416; the 32 team-defense slots have no box score and can never
    be owed a row here."""
    assert TEAM_COUNT == 32
    assert WATCHLIST_SLOTS_PER_TEAM == 2 + 3 + 4 + 2 + 1
    assert EXPECTED_FLOOR == {BOX_SIGNAL: 384}


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert len(EXPECTED_FLOOR) == len(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())


async def test_a_truncated_upstream_does_not_report_full_coverage():
    """The failure this floor exists for: an upstream returning one row of many
    must not yield `expected: 1, present: 1`, ratio 1.0."""
    envelope = (await capture([feed_row(position="WR", targets=1)]))[BOX_SIGNAL]
    assert envelope.coverage.expected == EXPECTED_FLOOR[BOX_SIGNAL]
    assert envelope.coverage.present == 1
    assert envelope.coverage.ratio < 0.01
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_an_empty_upstream_reports_zero_not_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing."""
    envelope = (await capture([]))[BOX_SIGNAL]
    assert envelope.coverage.present == 0
    assert envelope.coverage.ratio == 0.0


async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one — a real
    week carries several hundred non-watchlist players who took a snap."""
    rows = [
        feed_row(player_id=f"00-{index:07d}", position="WR", targets=1)
        for index in range(EXPECTED_FLOOR[BOX_SIGNAL] + 20)
    ]
    envelope = (await capture(rows))[BOX_SIGNAL]
    assert envelope.coverage.expected == len(rows)
    assert envelope.coverage.expected > EXPECTED_FLOOR[BOX_SIGNAL]
    assert envelope.coverage.ratio == 1.0


# ── what makes a key owed ─────────────────────────────────────────────────────


async def test_a_watchlist_player_with_no_row_is_missing_with_a_reason():
    """Expected because roster-scope names them, not because a row turned up.
    The spec: emit nothing, and count it in `coverage.missing` with a reason."""
    envelope = (
        await capture(
            [feed_row(player_id="00-0000001", position="WR", targets=1)],
            watchlist=["fdy-never-played"],
        )
    )[BOX_SIGNAL]
    assert "fdy-never-played" in envelope.coverage.missing
    assert envelope.coverage.present == 1
    assert "no_box_row" in {error["reason"] for error in envelope.errors}


async def test_a_watchlist_player_with_a_row_is_recorded_not_missing():
    """The join is the whole point of being scope-aware — if it silently never
    matched, every watchlist player would read as missing forever."""
    upstream_id = "00-0000001"
    envelope = (
        await capture(
            [feed_row(player_id=upstream_id, position="WR", targets=1)],
            watchlist=[stub_player_id(upstream_id)],
        )
    )[BOX_SIGNAL]
    assert envelope.coverage.present == 1
    assert envelope.coverage.missing == []


async def test_a_roster_scope_outage_fails_closed_before_touching_the_upstream():
    """No scope means zero upstream calls: the watchlist is fetched before
    the box-score feed, and a lake with nothing for this partition (never
    seeded — decision 5's own failure mode, `roster-scope` never having
    published) must not fall through to an unnarrowed capture."""
    lake = SpyLake()
    with respx.mock:
        route = respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(
                200, text=feed_csv([feed_row(position="WR", targets=1)])
            )
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(ScopeUnavailable):
                await capture_player_stats(
                    SEASON, WEEK, client=client, lake=lake, now=NOW
                )
    assert route.call_count == 0
    envelope = lake.writes[0]
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == EXPECTED_FLOOR[BOX_SIGNAL]
    assert "scope_unavailable" in {error["reason"] for error in envelope.errors}


async def test_a_lake_error_during_the_scope_fetch_still_fails_closed():
    """`ScopeUnavailable` is only what `ScopeClient` raises when the lake
    answered and had nothing usable. The lake can also fail outright — here,
    `ScopeClient._parse_captured_at` raises `ValueError` on a timestamp it
    does not recognise — and the capture's SECOND `except` arm around the
    scope fetch (a bare `Exception`, not just `ScopeUnavailable`) is what
    stops that escaping with no `present: 0` envelope and no
    `collector_capture_failures_total`."""
    lake = SpyLake()
    envelope = scope_envelope(["fdy-a"])
    body = envelope.to_dict()
    body["captured_at"] = "not-a-timestamp"
    lake.objects[lake_key(envelope)] = body
    with respx.mock:
        route = respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(
                200, text=feed_csv([feed_row(position="WR", targets=1)])
            )
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                await capture_player_stats(
                    SEASON, WEEK, client=client, lake=lake, now=NOW
                )
    assert route.call_count == 0
    failure_envelope = lake.writes[0]
    assert failure_envelope.coverage.present == 0
    assert failure_envelope.coverage.expected == EXPECTED_FLOOR[BOX_SIGNAL]
    # No `reason=` was passed on this arm, so it is whatever
    # `CollectorMetrics.reason_for` classifies a `ValueError` as — must not be
    # mistaken for `scope_unavailable`, which has a different fix.
    reasons = {error["reason"] for error in failure_envelope.errors}
    assert "scope_unavailable" not in reasons
    assert reasons, "a failure envelope with no errors explains nothing"


# ── rows that must not be emitted ─────────────────────────────────────────────


async def test_a_team_that_is_not_in_its_own_game_id_emits_nothing():
    """The spec's hard case: an upstream that keys a row to the player's
    CURRENT club rather than the one they played for. Ambiguous, so emit
    nothing and count it missing with a reason."""
    envelope = (
        await capture(
            [
                feed_row(
                    player_id="00-0000001",
                    position="WR",
                    targets=1,
                    team="DEN",
                    game_id=f"{SEASON}_01_BUF_KC",
                )
            ]
        )
    )[BOX_SIGNAL]
    assert envelope.signals == []
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == EXPECTED_FLOOR[BOX_SIGNAL]
    assert len(envelope.coverage.missing) == 1
    assert "team_game_mismatch" in {error["reason"] for error in envelope.errors}


async def test_a_duplicate_player_in_one_week_is_recorded_once():
    """One row per player per game, and a player plays one game a week. Two
    rows for the same player is the upstream contradicting itself."""
    envelope = (
        await capture(
            [
                feed_row(player_id="00-0000001", position="WR", receiving_yards=40),
                feed_row(player_id="00-0000001", position="WR", receiving_yards=90),
            ]
        )
    )[BOX_SIGNAL]
    assert len(envelope.signals) == 1
    assert envelope.coverage.present == 1
    assert "duplicate_row" in {error["reason"] for error in envelope.errors}


async def test_a_malformed_row_costs_one_key_not_the_pass():
    envelope = (
        await capture(
            [
                feed_row(player_id="00-0000001", position="WR", receiving_yards="?"),
                feed_row(player_id="00-0000002", position="WR", receiving_yards=90),
            ]
        )
    )[BOX_SIGNAL]
    assert len(envelope.signals) == 1
    assert "malformed_row" in {error["reason"] for error in envelope.errors}


async def test_an_unresolvable_identity_is_counted_not_dropped_silently():
    """The phase doc calls this the failure most likely to go unnoticed for a
    season: a capture that resolves 97% of rows looks healthy on every other
    metric."""
    envelope = (
        await capture(
            [
                feed_row(player_id="", position="WR", targets=1),
                feed_row(player_id="00-0000002", position="WR", targets=1),
            ]
        )
    )[BOX_SIGNAL]
    # A row with no player_id is refused by the adapter before the crosswalk
    # ever sees it, so the count lands as a malformed row rather than an
    # identity miss — either way it is one visible error, never a silent drop.
    assert len(envelope.signals) == 1
    assert envelope.errors


async def test_an_exhausted_deadline_truncates_and_says_so():
    """A truncated pass that reports itself truncated is useful; one that
    reports itself complete is not.

    The deadline is an absolute instant in the past, not `NOW` — `NOW` is the
    frozen instant the pass *describes* and is deliberately in the future
    relative to the test run, while a deadline is checked against a clock that
    actually advances.
    """
    rows = [
        feed_row(player_id=f"00-{index:07d}", position="WR", targets=1)
        for index in range(5)
    ]
    already_past = datetime(2020, 1, 1, tzinfo=UTC)
    envelope = (await capture(rows, deadline=already_past))[BOX_SIGNAL]
    assert envelope.signals == []
    assert "deadline_exceeded" in {error["reason"] for error in envelope.errors}
    assert envelope.coverage.ratio == 0.0

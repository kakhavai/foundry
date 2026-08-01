"""The coverage floor, and the `present` predicate that sits opposite it.

`coverage.expected` must never derive from what a fetch returned. These tests
are the ones that fail if somebody "simplifies" `capture.py` by counting the
games it managed to classify.

**Both halves are exercised deliberately.** A set that only attacks the floor
scores well and still misses a deleted `present` check: `acc.record` moving
above the `window_id is None` branch would make every unslotted game count as
covered, and every `expected`-side assertion stays green.
"""

from datetime import UTC, datetime, timedelta

import pytest

from broadcast_context.capture import EXPECTED_FLOOR, SIGNAL, SIGNAL_TYPES

from .conftest import (
    SpyLake,
    feed_document,
    feed_row,
    run_capture,
    season_rows,
    week_rows,
)


async def test_a_truncated_upstream_does_not_report_full_coverage():
    """The failure this floor exists for: an upstream returning one game of
    272 must not yield `expected: 1, present: 1`, ratio 1.0."""
    envelopes = await run_capture(
        feed_document([feed_row("2026_01_A_B")]), lake=SpyLake()
    )
    envelope = envelopes[SIGNAL]
    assert envelope.coverage.expected == EXPECTED_FLOOR[SIGNAL]
    assert envelope.coverage.present == 1
    assert envelope.coverage.ratio < 1.0
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_an_empty_upstream_reports_zero_not_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing."""
    envelopes = await run_capture(feed_document([]), lake=SpyLake())
    envelope = envelopes[SIGNAL]
    assert envelope.coverage.present == 0
    assert envelope.coverage.ratio == 0.0
    assert envelope.coverage.expected == EXPECTED_FLOOR[SIGNAL]


async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one.

    272 regular-season games plus a postseason is the real shape of a
    completed season, and it must read as more than 272 expected.
    """
    postseason = [
        feed_row(
            f"2026_19_P{i}_Q{i}",
            game_type="WC",
            week=19,
            gameday="2027-01-09",
            gametime=f"{13 + i}:00",
        )
        for i in range(6)
    ]
    envelopes = await run_capture(
        feed_document([*season_rows(), *postseason]), lake=SpyLake()
    )
    envelope = envelopes[SIGNAL]
    assert envelope.coverage.expected == 278
    assert envelope.coverage.ratio == 1.0


async def test_a_game_with_no_window_is_missing_not_defaulted():
    """The spec's own sentence, enforced on the `present` side.

    > a game whose window is not yet assigned counts as missing rather than
    > defaulting to `sun_early`.

    The row is still EMITTED — a consumer should see the game exists and that
    its window is unknown — but it is not counted as covered, and it carries
    `window_id: null` rather than a plausible default.
    """
    rows = [*week_rows(1, drop_kickoff_for=2), *week_rows(2)]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    envelope = envelopes[SIGNAL]

    assert len(envelope.signals) == 32, "the unslotted games must still be rows"
    assert envelope.coverage.present == 30
    assert len(envelope.coverage.missing) == 2
    unslotted = [row for row in envelope.signals if row["kickoff_at"] is None]
    assert len(unslotted) == 2
    assert all(row["window_id"] is None for row in unslotted)
    reasons = {error["reason"] for error in envelope.errors}
    assert "window_unassigned" in reasons, reasons


async def test_the_unslotted_games_are_named_in_missing():
    """`missing` must name the games, not merely count them — an operator
    fixing this needs to know which ones."""
    rows = [*week_rows(1, drop_kickoff_for=2), *week_rows(2)]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    assert set(envelopes[SIGNAL].coverage.missing) == {
        "2026_01_A00_B00",
        "2026_01_A01_B01",
    }


async def test_a_deadline_truncated_pass_reports_itself_truncated():
    """Over budget, the rest are recorded as missing rather than discarded: a
    truncated pass that reports itself truncated is useful; one that reports
    itself complete is not."""
    past = datetime.now(tz=UTC) - timedelta(seconds=1)
    envelopes = await run_capture(
        feed_document(season_rows()), lake=SpyLake(), deadline=past
    )
    envelope = envelopes[SIGNAL]
    assert envelope.signals == []
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == EXPECTED_FLOOR[SIGNAL]
    reasons = {error["reason"] for error in envelope.errors}
    assert "deadline_exceeded" in reasons, reasons


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())
    assert len(EXPECTED_FLOOR) == 1


def test_the_floor_is_the_league_schedule_not_a_fetch_artifact():
    """32 clubs x 17 games / 2. Pinned as a number so a change has to be
    deliberate — a floor quietly lowered to match a short fetch is the exact
    failure the floor exists to prevent, and it leaves every other test green.
    """
    assert EXPECTED_FLOOR[SIGNAL] == 272
    assert EXPECTED_FLOOR[SIGNAL] == 32 * 17 // 2


@pytest.mark.parametrize("games", [1, 5, 272])
async def test_present_never_exceeds_expected(games):
    rows = [
        feed_row(f"2026_01_A{i:03d}_B{i:03d}", week=1 + i // 16) for i in range(games)
    ]
    envelopes = await run_capture(feed_document(rows), lake=SpyLake())
    coverage = envelopes[SIGNAL].coverage
    assert coverage.present <= coverage.expected

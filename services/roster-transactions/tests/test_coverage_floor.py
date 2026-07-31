"""Coverage, which for this collector counts polling windows rather than rows.

`coverage.expected` must never derive from what a fetch returned. For an event
stream that trap is sharper than anywhere else in the fleet: transactions have
no fixed cardinality, a quiet Tuesday legitimately has zero, and
`expected = len(rows)` would therefore report a feed that returned **nothing**
as ratio 1.0 — which is the exact opposite of what a total outage means.

These are the tests that fail if somebody "simplifies" `capture.py` by counting
transactions instead of intervals.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from collector_core.streaming import UpstreamSchemaError

from roster_transactions.capture import (
    EXPECTED_FLOOR,
    SIGNAL_TYPE,
    capture_roster_transactions,
)
from roster_transactions.windows import (
    INTERVALS_PER_WEEK,
    elapsed_interval_keys,
    week_window,
)

from .conftest import SpyLake, capture_with, fake_window

SEASON, WEEK = 2026, 1
WEEK_START, WEEK_END = week_window(SEASON, WEEK)


async def test_a_quiet_week_with_zero_rows_is_still_full_coverage(monkeypatch):
    """The phase doc, verbatim: 'A quiet Tuesday with zero transactions is full
    coverage.' Coverage is over polling windows, so an acknowledged window with
    no moves in it is a collector doing its job perfectly."""
    now = WEEK_START + timedelta(days=2)
    envelopes = await capture_with(monkeypatch, rows=[], now=now, covers_through=now)

    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.signals == []
    assert envelope.coverage.present == envelope.coverage.expected
    assert envelope.coverage.expected == len(
        elapsed_interval_keys(SEASON, WEEK, now)
    ), "expected must count elapsed intervals, not rows"
    assert envelope.coverage.ratio == 1.0


async def test_expected_counts_intervals_not_transactions(monkeypatch):
    """The naive `expected = len(rows)` would make these two passes report the
    same number. They must not: the universe is time, and time did not change
    because the feed got busier."""
    now = WEEK_START + timedelta(days=3)
    quiet = await capture_with(monkeypatch, rows=[], now=now, covers_through=now)
    busy = await capture_with(
        monkeypatch,
        rows=[_row(hours=h) for h in range(1, 40)],
        now=now,
        covers_through=now,
    )

    assert busy[SIGNAL_TYPE].coverage.expected == quiet[SIGNAL_TYPE].coverage.expected
    assert len(busy[SIGNAL_TYPE].signals) == 39
    assert quiet[SIGNAL_TYPE].signals == []


async def test_a_total_outage_reports_a_low_ratio_not_a_perfect_one(monkeypatch):
    """The mandated test. A manifest that cannot be fetched must not floor
    `expected` to 1 and read as a near-miss — mid-week it owes hundreds of
    intervals, and the failure envelope has to say so."""

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("transaction wire down")

    monkeypatch.setattr("roster_transactions.capture.fetch_manifest", boom)
    now = WEEK_START + timedelta(days=4)
    owed = len(elapsed_interval_keys(SEASON, WEEK, now))
    lake = SpyLake()

    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as client:
            await capture_roster_transactions(
                SEASON, WEEK, client=client, lake=lake, now=now
            )

    assert lake.writes, "a failed capture must still write an envelope"
    assert len(lake.writes) == 1
    written = lake.writes[0]
    assert written.coverage.present == 0
    assert written.coverage.expected == owed > 1
    assert written.coverage.ratio == 0.0
    assert written.errors, "a failure envelope with no errors explains nothing"


async def test_a_truncated_acknowledgement_does_not_report_full_coverage(monkeypatch):
    """The truncation case, translated into this collector's units: the feed
    acknowledges only the first day of a week that is four days old."""
    now = WEEK_START + timedelta(days=4)
    envelopes = await capture_with(
        monkeypatch,
        rows=[_row(hours=2)],
        now=now,
        covers_through=WEEK_START + timedelta(days=1),
    )

    coverage = envelopes[SIGNAL_TYPE].coverage
    assert coverage.expected == len(elapsed_interval_keys(SEASON, WEEK, now))
    assert coverage.present == 4 * 24, "one acknowledged day is 96 intervals"
    assert coverage.ratio < 0.3
    reasons = {error["reason"] for error in envelopes[SIGNAL_TYPE].errors}
    assert "window_not_acknowledged" in reasons, reasons
    assert coverage.missing, "the unacknowledged intervals must be named"


async def test_a_week_not_yet_started_reports_zero_not_one(monkeypatch):
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a week this collector has never once polled."""
    now = WEEK_START - timedelta(hours=1)
    envelopes = await capture_with(monkeypatch, rows=[], now=now, covers_through=now)

    coverage = envelopes[SIGNAL_TYPE].coverage
    assert coverage.expected == EXPECTED_FLOOR[SIGNAL_TYPE] == 1
    assert coverage.present == 0
    assert coverage.ratio == 0.0


async def test_a_completed_week_expects_the_full_672_intervals(monkeypatch):
    """The floor never caps a genuine count, and a finished week is the whole
    universe: 7 days x 24 hours x 4."""
    now = WEEK_END + timedelta(days=3)
    envelopes = await capture_with(monkeypatch, rows=[], now=now, covers_through=now)

    coverage = envelopes[SIGNAL_TYPE].coverage
    assert coverage.expected == INTERVALS_PER_WEEK == 672
    assert coverage.present == INTERVALS_PER_WEEK
    assert coverage.ratio == 1.0


async def test_an_unmappable_row_poisons_its_interval(monkeypatch):
    """A row that will not map means the interval it belongs to is not
    trustworthy. Recording it present would let a wholesale schema break read
    as full coverage with a merely decorative errors array."""
    now = WEEK_START + timedelta(days=1)
    good = _row(hours=2)
    bad = _row(hours=5) | {"transaction_type": "mystery_move"}
    envelopes = await capture_with(
        monkeypatch, rows=[good, bad], now=now, covers_through=now
    )

    envelope = envelopes[SIGNAL_TYPE]
    assert len(envelope.signals) == 1
    coverage = envelope.coverage
    assert coverage.present < coverage.expected
    assert coverage.ratio < 1.0
    reasons = {error["reason"] for error in envelope.errors}
    assert reasons, "a poisoned interval with no error explains nothing"
    assert "malformed" in reasons, reasons


async def test_an_unplaceable_row_is_named_in_missing(monkeypatch):
    """Where the upstream is ambiguous, emit nothing and count it in
    `coverage.missing` with a reason — never a guess."""
    now = WEEK_START + timedelta(days=1)
    bad = _row(hours=3) | {"announced_at": "not-a-timestamp"}
    envelopes = await capture_with(monkeypatch, rows=[bad], now=now, covers_through=now)

    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.signals == []
    row_keys = [key for key in envelope.coverage.missing if key.startswith("row:")]
    assert len(row_keys) == 1, envelope.coverage.missing
    assert row_keys[0] == "row:wire/3"


def test_every_signal_type_declares_a_floor():
    from roster_transactions.capture import SIGNAL_TYPES

    assert len(SIGNAL_TYPES) == 1
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    floors = list(EXPECTED_FLOOR.values())
    assert len(floors) == len(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in floors)


def test_the_manifest_refuses_a_backwards_window():
    """A swapped pair would turn a healthy feed into a reported total outage,
    so it is refused where it is constructed rather than downstream."""
    from roster_transactions.adapters.upstream import CoverageWindow

    with pytest.raises(UpstreamSchemaError):
        CoverageWindow(
            covers_from=datetime(2026, 9, 2, tzinfo=UTC),
            covers_through=datetime(2026, 9, 1, tzinfo=UTC),
            feed_url="",
        )


def test_fake_window_helper_is_actually_used():
    """Guards the helper the rest of this file leans on."""
    window = fake_window(WEEK_START, WEEK_START + timedelta(days=1))
    assert window.covers_through > window.covers_from


def _row(*, hours: int) -> dict[str, str]:
    """One raw upstream row, announced `hours` into the scoped week."""
    announced = WEEK_START + timedelta(hours=hours)
    return {
        "transaction_type": "signing",
        "player_id": f"fdy-{hours:04d}",
        "position": "WR",
        "from_team": "",
        "to_team": "KC",
        "announced_at": announced.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "effective_at": (announced + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "eligible_from_week": "",
        "elevation_count_season": "",
        "confidence": "official",
        "is_void": "false",
        "void_reason": "",
        "supersedes": "",
        "source_ref": f"wire/{hours}",
    }

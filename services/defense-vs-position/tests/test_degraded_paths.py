"""The paths a healthy week never takes: the deadline, and one bad row.

Both produce a published envelope rather than an exception, so neither shows
up as a failure anywhere -- they show up as coverage, or not at all if the
accounting is wrong.
"""

from datetime import UTC, datetime, timedelta

import pytest

from defense_vs_position.adapters.pbp import _flag, _num

from .conftest import SpyLake, run_capture

SIGNAL_TYPE = "defense_positional_allowance"


async def test_a_deadline_already_passed_records_every_row_missing(upstreams):
    """Over budget: record the rest as missing rather than throwing away what
    already resolved. A truncated pass that reports itself truncated is
    useful; one that reports itself complete is not."""
    envelope = (
        await run_capture(
            SpyLake(), deadline=datetime.now(tz=UTC) - timedelta(seconds=1)
        )
    )[SIGNAL_TYPE]

    assert envelope.signals == []
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == 32
    reasons = {e["reason"] for e in envelope.errors}
    assert "deadline_exceeded" in reasons, reasons


async def test_a_generous_deadline_does_not_truncate(upstreams):
    """The other arm: a deadline that has not passed must change nothing. A
    comparison with the wrong sense passes the test above and fails here."""
    envelope = (
        await run_capture(SpyLake(), deadline=datetime.now(tz=UTC) + timedelta(hours=1))
    )[SIGNAL_TYPE]
    assert len(envelope.signals) == 576
    assert envelope.coverage.present == 32


async def test_one_unbuildable_row_does_not_lose_the_pass(upstreams, monkeypatch):
    """One bad row is a coverage failure for its key, not a failed capture.

    The defense it belongs to stops being present -- it no longer has a
    complete set -- while the other 31 publish normally.
    """
    from defense_vs_position import capture as capture_module

    original = capture_module.build_signal

    def explode(signal_type, row, *, now):
        if row["team_id"] == "PHI" and row["position"] == "WR":
            raise ValueError("row is malformed")
        return original(signal_type, row, now=now)

    monkeypatch.setattr(capture_module, "build_signal", explode)
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    assert len(envelope.signals) == 576 - 3, "only the three PHI/WR rows are lost"
    assert envelope.coverage.present == 31
    assert envelope.coverage.missing == ["PHI"]
    assert any(
        e["reason"] == "malformed" and e["detail"] == "PHI" for e in envelope.errors
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1.0), ("1.0", 1.0), ("", 0.0), ("NA", 0.0), ("  ", 0.0), ("?", 0.0)],
)
def test_a_numeric_cell_falls_back_to_zero_rather_than_raising(raw, expected):
    """Every column read through `_num` is a counting stat or a yardage, so a
    blank means the play did not have that thing. An unparseable cell must not
    kill a 48,000-row pass."""
    assert _num(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("1.0", True), ("0", False), ("", False), ("NA", False)],
)
def test_a_flag_cell_is_true_only_at_exactly_one(raw, expected):
    assert _flag(raw) is expected

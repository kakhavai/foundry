"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These tests
are the ones that fail if somebody "simplifies" `capture.py` by computing the
expectation from `rows`.
"""

import httpx

from depth_chart.capture import (
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    capture_depth_chart,
)

from .conftest import NOW, SpyLake


async def _capture(rows, monkeypatch, lake):
    async def fake(*args, **kwargs):
        return list(rows)

    monkeypatch.setattr("depth_chart.capture.fetch_rows", fake)
    async with httpx.AsyncClient() as client:
        return await capture_depth_chart(
            2026, 1, client=client, lake=lake, now=NOW
        )


async def test_a_truncated_upstream_does_not_report_full_coverage(monkeypatch):
    """The failure this floor exists for: an upstream returning one row of many
    must not yield `expected: 1, present: 1`, ratio 1.0."""
    envelopes = await _capture(
        [{"key": "only-one", "value": 1.0}], monkeypatch, SpyLake()
    )
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.present == 1
        assert envelope.coverage.ratio < 1.0
        reasons = {error["reason"] for error in envelope.errors}
        assert "below_expected_floor" in reasons, reasons


async def test_an_empty_upstream_reports_zero_not_one(monkeypatch):
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing."""
    envelopes = await _capture([], monkeypatch, SpyLake())
    for envelope in envelopes.values():
        assert envelope.coverage.present == 0
        assert envelope.coverage.ratio == 0.0


async def test_expansion_past_the_floor_still_reports_honestly(monkeypatch):
    """The floor must not CAP a genuine count, only raise a short one."""
    rows = [{"key": f"k{i}", "value": float(i)} for i in range(50)]
    envelopes = await _capture(rows, monkeypatch, SpyLake())
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == max(50, EXPECTED_FLOOR[signal_type])
        assert envelope.coverage.ratio == 1.0


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())

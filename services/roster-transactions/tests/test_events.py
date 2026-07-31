"""Cursor paging, without the HTTP layer.

The route body is a call and a return; everything with a decision in it is here.
"""

import pytest

from roster_transactions.events import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursor,
    cursor_for,
    page,
)


def _row(announced: str, transaction_id: str) -> dict:
    return {"announced_at": announced, "transaction_id": transaction_id}


ROWS = [
    _row("2026-09-03T00:00:00Z", "rtx-cccccccccccccccc"),
    _row("2026-09-01T00:00:00Z", "rtx-aaaaaaaaaaaaaaaa"),
    _row("2026-09-02T00:00:00Z", "rtx-bbbbbbbbbbbbbbbb"),
]


def test_an_empty_stream_pages_to_nothing():
    assert page([]) == {"events": [], "count": 0, "next_cursor": None}


def test_rows_come_back_in_announced_order_whatever_order_they_arrived():
    body = page(ROWS)
    assert body["count"] == 3
    announced = [row["announced_at"] for row in body["events"]]
    assert len(announced) == 3
    assert announced == sorted(announced)


def test_the_tiebreak_is_not_optional():
    """A trade is two rows at the same instant. A cursor over a non-total order
    either re-delivers or skips, silently, depending on how the sort fell."""
    same_instant = [
        _row("2026-09-01T00:00:00Z", "rtx-2222222222222222"),
        _row("2026-09-01T00:00:00Z", "rtx-1111111111111111"),
    ]
    first = page(same_instant, limit=1)
    assert first["count"] == 1
    assert first["events"][0]["transaction_id"] == "rtx-1111111111111111"

    second = page(same_instant, limit=1, since=first["next_cursor"])
    assert second["count"] == 1
    assert second["events"][0]["transaction_id"] == "rtx-2222222222222222"
    assert second["next_cursor"] is None


def test_paging_neither_repeats_nor_drops_a_row():
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        body = page(ROWS, limit=1, since=cursor)
        seen.extend(row["transaction_id"] for row in body["events"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 3
    assert len(set(seen)) == 3


def test_a_cursor_past_the_end_returns_an_empty_page():
    body = page(ROWS, since=cursor_for(ROWS[0]))
    assert body["events"] == []
    assert body["count"] == 0
    assert body["next_cursor"] is None


def test_next_cursor_is_none_only_when_the_stream_is_exhausted():
    """A consumer polls until it gets None rather than guessing from a short
    page — a page can be short because `limit` landed on the boundary."""
    assert page(ROWS, limit=3)["next_cursor"] is None
    assert page(ROWS, limit=2)["next_cursor"] is not None


def test_a_malformed_cursor_is_rejected_not_silently_restarted():
    """Silently restarting from the beginning re-delivers a whole week."""
    for bad in ["garbage", "|rtx-a", "2026-09-01T00:00:00Z|", ""]:
        with pytest.raises(InvalidCursor):
            page(ROWS, since=bad)


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
def test_an_out_of_range_limit_is_rejected(limit):
    with pytest.raises(ValueError):
        page(ROWS, limit=limit)


def test_the_default_limit_is_within_the_maximum():
    assert 1 <= DEFAULT_LIMIT <= MAX_LIMIT


def test_a_cursor_round_trips():
    row = ROWS[1]
    assert cursor_for(row) == "2026-09-01T00:00:00Z|rtx-aaaaaaaaaaaaaaaa"
    body = page(ROWS, since=cursor_for(row))
    assert [r["transaction_id"] for r in body["events"]] == [
        "rtx-bbbbbbbbbbbbbbbb",
        "rtx-cccccccccccccccc",
    ]

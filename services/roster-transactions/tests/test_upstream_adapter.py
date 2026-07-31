"""The real-upstream branches, which the placeholder otherwise hides.

These are the lines that actually run in production, and nothing else in the
suite reaches them: with `UPSTREAM_URL` empty every other test takes the
placeholder path. `UPSTREAM_URL` is patched on the module rather than through an
environment variable because it is a module constant by design — the adapter is
the only place the wire format is known, and that includes where it lives.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.streaming import UpstreamSchemaError

from roster_transactions.adapters import upstream as adapter
from roster_transactions.windows import week_window

BASE = "https://wire.example/transactions"
MANIFEST = f"{BASE}/2026/manifest.json"
FEED = "https://wire.example/transactions/2026/feed.csv"
WEEK_START, WEEK_END = week_window(2026, 1)

COLUMNS = [
    "transaction_type",
    "player_id",
    "position",
    "from_team",
    "to_team",
    "announced_at",
    "effective_at",
    "confidence",
    "is_void",
    "void_reason",
    "supersedes",
    "source_ref",
]


@pytest.fixture(autouse=True)
def _real_upstream(monkeypatch):
    monkeypatch.setattr(adapter, "UPSTREAM_URL", BASE)


def _csv(rows: list[list[str]], columns: list[str] | None = None) -> str:
    header = ",".join(columns if columns is not None else COLUMNS)
    return "\n".join([header, *(",".join(row) for row in rows)]) + "\n"


def _row(announced: datetime, player: str = "fdy-0001") -> list[str]:
    stamp = announced.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        "signing",
        player,
        "WR",
        "",
        "KC",
        stamp,
        stamp,
        "official",
        "false",
        "",
        "",
        f"wire/{player}",
    ]


def test_source_ref_names_the_manifest():
    """The manifest is the artifact named because it carries the
    acknowledgement the whole coverage block is computed from."""
    assert adapter.source_ref(2026, 1) == MANIFEST


@respx.mock
async def test_the_manifest_is_parsed_into_a_coverage_window():
    respx.get(MANIFEST).mock(
        return_value=httpx.Response(
            200,
            json={
                "covers_from": "2026-09-01T00:00:00Z",
                "covers_through": "2026-09-04T00:00:00Z",
                "feed_url": FEED,
            },
        )
    )
    async with httpx.AsyncClient() as client:
        window = await adapter.fetch_manifest(2026, 1, client=client, now=WEEK_START)

    assert window.covers_from == datetime(2026, 9, 1, tzinfo=UTC)
    assert window.covers_through == datetime(2026, 9, 4, tzinfo=UTC)
    assert window.feed_url == FEED


@respx.mock
async def test_a_manifest_missing_a_field_fails_loudly():
    """Defaulting the window would be the worst possible failure here: it would
    silently claim the feed covered everything."""
    respx.get(MANIFEST).mock(
        return_value=httpx.Response(200, json={"covers_from": "2026-09-01T00:00:00Z"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamSchemaError) as caught:
            await adapter.fetch_manifest(2026, 1, client=client, now=WEEK_START)
    assert "covers_through" in str(caught.value)
    assert "feed_url" in str(caught.value)


@respx.mock
async def test_a_manifest_that_is_not_an_object_fails_loudly():
    respx.get(MANIFEST).mock(return_value=httpx.Response(200, json=["nope"]))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamSchemaError):
            await adapter.fetch_manifest(2026, 1, client=client, now=WEEK_START)


@respx.mock
async def test_an_unreachable_manifest_raises_rather_than_defaulting():
    respx.get(MANIFEST).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.fetch_manifest(2026, 1, client=client, now=WEEK_START)


async def _collect(rows_csv: str) -> list[dict]:
    window = adapter.CoverageWindow(WEEK_START, WEEK_END, FEED)
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rows_csv))
    async with httpx.AsyncClient() as client:
        return [
            row
            async for row in adapter.stream_rows(
                2026, 1, client=client, window=window, now=WEEK_END
            )
        ]


@respx.mock
async def test_rows_outside_the_scoped_week_are_dropped_as_they_are_parsed():
    """Filter as you parse, not after. A season-long wire is mostly rows this
    pass will not keep, and materialising them first is the roster-scope OOM
    verbatim."""
    collected = await _collect(
        _csv(
            [
                _row(WEEK_START - timedelta(days=30), "fdy-early"),
                _row(WEEK_START + timedelta(hours=6), "fdy-inside"),
                _row(WEEK_END + timedelta(days=30), "fdy-late"),
            ]
        )
    )
    assert len(collected) == 1
    assert collected[0]["player_id"] == "fdy-inside"


@respx.mock
async def test_the_week_boundary_is_half_open():
    """A row at the closing instant belongs to the NEXT week, or it would be
    captured twice."""
    collected = await _collect(
        _csv(
            [
                _row(WEEK_START, "fdy-open"),
                _row(WEEK_END, "fdy-close"),
            ]
        )
    )
    assert [row["player_id"] for row in collected] == ["fdy-open"]


@respx.mock
async def test_a_renamed_column_fails_before_any_row_is_mapped():
    """Validate before mapping, so a renamed field fails with `schema` rather
    than writing nulls into a lake nobody rewrites."""
    renamed = [c if c != "announced_at" else "reported_at" for c in COLUMNS]
    window = adapter.CoverageWindow(WEEK_START, WEEK_END, FEED)
    respx.get(FEED).mock(
        return_value=httpx.Response(
            200, text=_csv([_row(WEEK_START + timedelta(hours=1))], renamed)
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamSchemaError) as caught:
            async for _ in adapter.stream_rows(
                2026, 1, client=client, window=window, now=WEEK_END
            ):
                pass
    assert "announced_at" in str(caught.value)


@respx.mock
async def test_an_unparseable_timestamp_is_yielded_not_silently_dropped():
    """A row quietly discarded by an adapter is indistinguishable from a row the
    upstream never sent. `capture.py` turns it into a counted failure instead."""
    broken = _row(WEEK_START + timedelta(hours=2), "fdy-broken")
    broken[COLUMNS.index("announced_at")] = "whenever"
    collected = await _collect(_csv([broken]))
    assert len(collected) == 1
    assert collected[0]["announced_at"] == "whenever"


@respx.mock
async def test_a_row_with_an_empty_timestamp_is_yielded_for_capture_to_classify():
    blank = _row(WEEK_START + timedelta(hours=2), "fdy-blank")
    blank[COLUMNS.index("announced_at")] = ""
    collected = await _collect(_csv([blank]))
    assert len(collected) == 1
    assert collected[0]["player_id"] == "fdy-blank"


async def test_the_feed_is_streamed_rather_than_buffered(monkeypatch):
    """Peak memory is one chunk plus one row, independent of document size.

    Asserted structurally, because no assertion on the returned rows can tell a
    streamed read from a buffered one — and a 36.8 MB document buffered three
    times is precisely what OOMKilled roster-scope while its 171 tests stayed
    green. What this pins is that the adapter delegates to the shared streaming
    reader, with the schema guard armed. Swapping in `response.json()` or
    `response.text` would fail here rather than in a 256Mi pod.
    """
    calls: list[tuple] = []

    async def spy(client, url, *, required_columns=None, **kwargs):
        calls.append((url, required_columns))
        for n in range(24):
            yield dict(zip(COLUMNS, _row(WEEK_START + timedelta(hours=n)), strict=True))

    monkeypatch.setattr(adapter, "stream_csv_dicts", spy)
    window = adapter.CoverageWindow(WEEK_START, WEEK_END, FEED)
    async with httpx.AsyncClient() as client:
        rows = [
            row
            async for row in adapter.stream_rows(
                2026, 1, client=client, window=window, now=WEEK_END
            )
        ]

    assert len(rows) == 24
    assert len(calls) == 1
    url, required = calls[0]
    assert url == FEED
    assert required == adapter.REQUIRED_COLUMNS
    assert "announced_at" in required, "the schema guard must actually be armed"

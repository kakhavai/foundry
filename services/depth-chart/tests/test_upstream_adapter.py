"""The adapter: streaming, filtering, bounded retention, and schema drift.

Every test here runs the **real** `fetch_depth_charts` over a mocked transport,
so the streaming path, the chunk stitching and the week-bucket eviction are all
exercised rather than assumed.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.streaming import UpstreamSchemaError

from depth_chart.adapters.upstream import (
    MAX_WEEKS,
    DepthChartTooLarge,
    fetch_depth_charts,
    parse_dt,
    source_ref,
)

from .conftest import DEPTH_HEADER, WEEK_DTS, depth_csv, depth_row

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
FEED = source_ref(2026, 1)


async def fetch(rows, *, header=DEPTH_HEADER, deadline=None, url=FEED):
    respx.get(url).mock(return_value=httpx.Response(200, text=depth_csv(rows, header)))
    async with httpx.AsyncClient() as client:
        return await fetch_depth_charts(
            2026, 1, client=client, now=NOW, deadline=deadline
        )


def test_source_ref_substitutes_the_season():
    """A URL without the `{season}` placeholder would pin every capture to
    whichever season was baked in — the feed is one asset per season."""
    assert "depth_charts_2026.csv" in source_ref(2026, 1)
    assert "depth_charts_2025.csv" in source_ref(2025, 9)


@respx.mock
async def test_rows_are_grouped_by_team_and_canonical_position():
    fetched = await fetch(
        [
            depth_row("KC", "QB", 1, "A Passer"),
            depth_row("JAC", "HB", 1, "A Runner"),
        ]
    )
    assert set(fetched.histories) == {"KC:QB", "JAX:RB"}
    assert fetched.histories["JAX:RB"].current.entries[0].player_name == "A Runner"


@respx.mock
async def test_an_unknown_team_is_dropped_rather_than_passed_through():
    """Its groups then read as missing, which is the correct accounting for
    'we have no chart for this team'."""
    fetched = await fetch([depth_row("XYZ", "QB", 1, "Nobody")])
    assert fetched.histories == {}
    assert fetched.rows_kept == 0


@respx.mock
async def test_rows_outside_the_configured_positions_are_dropped_at_ingest():
    """A memory decision, not a modelling one: the feed carries the full
    two-deep for the offensive line, both defensive fronts and the rest of
    special teams, and this collector publishes five position groups."""
    fetched = await fetch(
        [
            depth_row("KC", "QB", 1, "A Passer"),
            depth_row("KC", "LDE", 1, "An End"),
            depth_row("KC", "LT", 1, "A Tackle"),
            depth_row("KC", "PK", 1, "A Kicker"),
        ]
    )
    assert set(fetched.histories) == {"KC:QB", "KC:K"}
    assert fetched.rows_kept == 2


@respx.mock
async def test_unranked_nameless_and_undated_rows_are_dropped():
    fetched = await fetch(
        [
            depth_row("KC", "QB", 1, "Real Player"),
            f"{WEEK_DTS[-1]},KC,No Rank,1,x,1,G,1,N,QB,1,",
            f"{WEEK_DTS[-1]},KC,,1,x,1,G,1,N,QB,2,2",
            ",KC,No Date,1,x,1,G,1,N,QB,3,3",
        ]
    )
    names = [e.player_name for e in fetched.histories["KC:QB"].current.entries]
    assert names == ["Real Player"]


@respx.mock
async def test_a_renamed_column_fails_loudly_before_any_row_is_mapped():
    """Schema drift must fail with `reason=malformed` rather than writing
    nulls into an append-only lake nobody rewrites."""
    header = DEPTH_HEADER.replace("pos_rank", "position_rank")
    with pytest.raises(UpstreamSchemaError) as excinfo:
        await fetch([depth_row("KC", "QB", 1, "A Passer")], header=header)
    assert "pos_rank" in str(excinfo.value)


@respx.mock
async def test_an_error_status_raises_rather_than_returning_nothing():
    """An empty result would be recorded as a successful capture of nothing."""
    respx.get(FEED).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_depth_charts(2026, 1, client=client, now=NOW)


@respx.mock
async def test_the_newest_publication_in_a_week_supersedes_the_older_one():
    """Merging two publications inside one week would splice two orderings
    together and invent a chart nobody published."""
    early, late = "2026-09-07T07:00:00Z", "2026-09-09T07:00:00Z"
    fetched = await fetch(
        [
            depth_row("KC", "QB", 1, "Old Starter", dt=early),
            depth_row("KC", "QB", 2, "Old Backup", dt=early),
            depth_row("KC", "QB", 1, "New Starter", dt=late),
        ]
    )
    current = fetched.histories["KC:QB"].current
    assert [e.player_name for e in current.entries] == ["New Starter"]
    assert current.captured_at == parse_dt(late)


@respx.mock
async def test_history_is_bounded_to_max_weeks_regardless_of_feed_length():
    """The retention bound is what keeps peak memory independent of how far
    back the season file goes. Without it a 53 MB feed is ~350 snapshots deep."""
    base = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)
    rows = [
        depth_row(
            "KC",
            "QB",
            1,
            f"Passer {index}",
            dt=(base + timedelta(weeks=index)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for index in range(MAX_WEEKS * 3)
    ]
    fetched = await fetch(rows)
    history = fetched.histories["KC:QB"]
    assert len(history.weeks) == MAX_WEEKS
    # Newest first, and the newest is the last row the feed published.
    assert history.current.entries[0].player_name == f"Passer {MAX_WEEKS * 3 - 1}"


@respx.mock
async def test_a_row_older_than_the_eviction_horizon_cannot_resurrect_a_bucket():
    """Retention must not depend on the feed arriving in `dt` order. An
    out-of-order straggler for an evicted week would otherwise recreate that
    week with a single player in it and report a fabricated ordering."""
    base = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)

    def stamp(index):
        return (base + timedelta(weeks=index)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = [
        depth_row("KC", "QB", 1, f"Passer {index}", dt=stamp(index))
        for index in range(MAX_WEEKS + 2)
    ]
    rows.append(depth_row("KC", "QB", 1, "Straggler", dt=stamp(0)))

    fetched = await fetch(rows)
    history = fetched.histories["KC:QB"]
    assert len(history.weeks) == MAX_WEEKS
    kept = {e.player_name for week in history.weeks for e in week.entries}
    assert "Straggler" not in kept
    assert len(kept) == MAX_WEEKS


@respx.mock
async def test_a_straggler_older_than_a_full_history_is_dropped_not_kept():
    """The eviction horizon's other entry point: the buckets fill up without
    anything ever being evicted, then an ancient row arrives. It must be
    dropped, not swapped in for a newer week."""
    base = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)

    def stamp(index):
        return (base + timedelta(weeks=index)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = [
        depth_row("KC", "QB", 1, f"Passer {index}", dt=stamp(index))
        for index in range(1, MAX_WEEKS + 1)
    ]
    rows.append(depth_row("KC", "QB", 1, "Ancient", dt=stamp(0)))

    fetched = await fetch(rows)
    history = fetched.histories["KC:QB"]
    assert len(history.weeks) == MAX_WEEKS
    kept = {e.player_name for week in history.weeks for e in week.entries}
    assert "Ancient" not in kept
    assert len(kept) == MAX_WEEKS


@respx.mock
async def test_an_older_publication_arriving_late_does_not_overwrite_a_newer_one():
    """Feed order is not guaranteed. A stale row for the same week arriving
    after the newer one must be ignored rather than reinstating a superseded
    ordering."""
    early, late = "2026-09-07T07:00:00Z", "2026-09-09T07:00:00Z"
    fetched = await fetch(
        [
            depth_row("KC", "QB", 1, "New Starter", dt=late),
            depth_row("KC", "QB", 1, "Old Starter", dt=early),
        ]
    )
    current = fetched.histories["KC:QB"].current
    assert [e.player_name for e in current.entries] == ["New Starter"]
    assert current.captured_at == parse_dt(late)


@respx.mock
async def test_lines_split_across_chunk_boundaries_are_stitched():
    """`aiter_text` splits on transport chunks, not on newlines. A row cut in
    half must not be dropped or half-parsed."""
    rows = [depth_row("KC", "QB", rank, f"Passer {rank}") for rank in range(1, 40)]
    fetched = await fetch(rows)
    assert len(fetched.histories["KC:QB"].current.entries) == 39


@respx.mock
async def test_a_deadline_truncates_the_stream_and_says_so():
    """A truncated pass that reports itself truncated is useful; one that
    reports itself complete is the silent failure coverage exists to catch.

    The deadline is taken from the **wall clock**, not from the suite's frozen
    `NOW`: `capture_deadline` is real elapsed budget, and passing `NOW - 1s`
    here silently tested nothing, because `NOW` is in the suite's future.
    """
    fetched = await fetch(
        [depth_row("KC", "QB", 1, "A Passer")],
        deadline=datetime.now(tz=UTC) - timedelta(seconds=1),
    )
    assert fetched.truncated is True
    assert fetched.histories == {}


@respx.mock
async def test_an_unbounded_feed_is_refused(monkeypatch):
    monkeypatch.setattr("depth_chart.adapters.upstream.MAX_ROWS", 3)
    rows = [depth_row("KC", "QB", rank, f"Passer {rank}") for rank in range(1, 12)]
    with pytest.raises(DepthChartTooLarge):
        await fetch(rows)


@respx.mock
async def test_an_empty_document_is_a_schema_error_not_an_empty_chart():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=""))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamSchemaError):
            await fetch_depth_charts(2026, 1, client=client, now=NOW)


def test_parse_dt_rejects_garbage_rather_than_substituting_now():
    """Substituting the fetch instant would make a frozen chart look freshly
    published, which is the exact failure this collector must detect."""
    assert parse_dt("not-a-date") is None
    assert parse_dt("") is None
    assert parse_dt(None) is None
    assert parse_dt("2026-09-08T07:00:00Z") == datetime(2026, 9, 8, 7, 0, tzinfo=UTC)


def test_a_naive_timestamp_is_read_as_utc():
    parsed = parse_dt("2026-09-08T07:00:00")
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed == datetime(2026, 9, 8, 7, 0, tzinfo=UTC)

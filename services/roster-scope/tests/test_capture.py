"""Capture orchestration: versions, the outage paths, and the lake."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from roster_scope.capture import capture_scope
from roster_scope.scope import CHANGE_SIGNAL, MEMBERSHIP_SIGNAL

from .conftest import NOW, SpyLake, depth_csv, depth_row, full_league_csv

FEED = "https://feed.test/{season}.csv"
FEED_2026 = "https://feed.test/2026.csv"


@pytest.fixture(autouse=True)
def _feed_url(monkeypatch):
    monkeypatch.setattr("roster_scope.adapters.depth_chart.DEPTH_CHART_URL", FEED)


def mock_feed(text: str):
    return respx.get(FEED_2026).mock(return_value=httpx.Response(200, text=text))


async def run_capture(lake, *, week=1, now=NOW, deadline=None):
    async with httpx.AsyncClient() as client:
        return await capture_scope(
            2026, week, client=client, lake=lake, now=now, deadline=deadline
        )


@respx.mock
async def test_a_complete_capture_writes_both_envelopes(lake):
    mock_feed(full_league_csv())
    result = await run_capture(lake)

    assert set(result) == {MEMBERSHIP_SIGNAL, CHANGE_SIGNAL}
    membership = result[MEMBERSHIP_SIGNAL]
    assert membership.coverage.expected == 416
    assert membership.coverage.present == 416
    assert membership.coverage.ratio == 1.0
    assert membership.errors == []
    assert membership.scope == {"season": 2026, "week": 1, "scope_version": 1}
    assert len(lake.writes) == 2


@respx.mock
async def test_upstream_provenance_is_recorded(lake):
    mock_feed(full_league_csv())
    result = await run_capture(lake)
    upstream = result[MEMBERSHIP_SIGNAL].upstream
    assert upstream.adapter == "nflverse-depth-charts"
    assert upstream.source_ref == FEED_2026
    assert upstream.fetched_at == NOW


@respx.mock
async def test_the_first_capture_emits_entered_for_everything(lake):
    mock_feed(full_league_csv())
    result = await run_capture(lake)
    events = result[CHANGE_SIGNAL].signals
    assert len(events) == 416
    assert {e["transition"] for e in events} == {"entered"}
    # A change stream is derived, not captured, so it carries no coverage
    # expectation of its own.
    assert result[CHANGE_SIGNAL].coverage.to_dict() == {
        "expected": 0,
        "present": 0,
        "missing": [],
    }


@respx.mock
async def test_the_version_increments_from_the_lake_not_from_memory(lake):
    """A restarted pod that reset to 1 would break the immutable-additive
    model the whole fleet pins against."""
    mock_feed(full_league_csv())
    first = await run_capture(lake)
    assert first[MEMBERSHIP_SIGNAL].scope["scope_version"] == 1

    second = await run_capture(lake, now=NOW + timedelta(hours=1))
    assert second[MEMBERSHIP_SIGNAL].scope["scope_version"] == 2
    assert all(r["scope_version"] == 2 for r in second[MEMBERSHIP_SIGNAL].signals)


@respx.mock
async def test_the_version_carries_across_a_week_boundary(lake):
    mock_feed(full_league_csv())
    await run_capture(lake)
    respx.get("https://feed.test/2026.csv").mock(
        return_value=httpx.Response(200, text=full_league_csv(week=2))
    )
    second = await run_capture(lake, week=2, now=NOW + timedelta(days=7))
    assert second[MEMBERSHIP_SIGNAL].scope["scope_version"] == 2


@respx.mock
async def test_a_rank_change_between_captures_emits_one_event(lake):
    """The chart flips KC's top two receivers; nothing else moves."""
    mock_feed(full_league_csv())
    await run_capture(lake)

    swapped = full_league_csv().replace(
        "2026,1,KC,WR,1,KC WR Player1,11,\n2026,1,KC,WR,2,KC WR Player2,12,",
        "2026,1,KC,WR,1,KC WR Player2,11,\n2026,1,KC,WR,2,KC WR Player1,12,",
    )
    respx.get(FEED_2026).mock(return_value=httpx.Response(200, text=swapped))
    second = await run_capture(lake, now=NOW + timedelta(hours=1))

    events = second[CHANGE_SIGNAL].signals
    assert {e["transition"] for e in events} == {"rank_changed"}
    assert len(events) == 2
    assert second[MEMBERSHIP_SIGNAL].coverage.present == 416


@respx.mock
async def test_a_total_upstream_outage_still_writes_an_envelope(lake):
    """`weather` raises here and writes nothing. This collector does not: a
    gap in an append-only lake must be explicit, never inferred from absence."""
    respx.get(FEED_2026).mock(return_value=httpx.Response(503))
    result = await run_capture(lake)

    membership = result[MEMBERSHIP_SIGNAL]
    assert membership.coverage.expected == 416
    assert membership.coverage.present == 32
    assert round(membership.coverage.ratio, 3) == 0.077
    assert len(lake.writes) == 2

    reasons = {e["reason"] for e in membership.errors}
    assert "http_status" in reasons, "the fetch failure itself is classified"
    assert "depth_chart_unavailable" in reasons
    assert any(e.get("detail") == "depth_chart_fetch" for e in membership.errors)


@respx.mock
async def test_a_connection_failure_is_classified_as_transport(lake):
    respx.get(FEED_2026).mock(side_effect=httpx.ConnectError("refused"))
    result = await run_capture(lake)
    assert any(e["reason"] == "transport" for e in result[MEMBERSHIP_SIGNAL].errors)


@respx.mock
async def test_schema_drift_is_classified_as_malformed(lake):
    """An upstream that renames a column must fail loudly rather than map
    nulls into the lake."""
    mock_feed("season,week,club_code,depth_position,depth_team\n2026,1,KC,QB,1\n")
    result = await run_capture(lake)
    assert any(e["reason"] == "malformed" for e in result[MEMBERSHIP_SIGNAL].errors)
    assert result[MEMBERSHIP_SIGNAL].coverage.present == 32


@respx.mock
async def test_a_ledger_read_failure_mints_no_version(lake):
    """`scope_version: 0`, `present: 0`, errors populated — never a reset to 1,
    which would collide with a real version 1 already in the lake."""
    mock_feed(full_league_csv())
    broken = SpyLake(fail_list=True)
    result = await run_capture(broken)

    for envelope in result.values():
        assert envelope.scope["scope_version"] == 0
        assert envelope.signals == []
        assert [e["reason"] for e in envelope.errors] == ["ledger_unavailable"]

    membership = result[MEMBERSHIP_SIGNAL]
    assert membership.coverage.expected == 416
    assert membership.coverage.present == 0
    assert len(membership.coverage.missing) == 416
    # It still writes: the failure is recorded in the lake, not inferred from
    # a hole in it.
    assert len(broken.writes) == 2


@respx.mock
async def test_a_ledger_failure_does_not_fetch_the_upstream(lake):
    """No version can be minted, so hitting the upstream would spend a call
    on a pass that cannot produce anything."""
    route = mock_feed(full_league_csv())
    await run_capture(SpyLake(fail_list=True))
    assert route.call_count == 0


@respx.mock
async def test_a_lake_write_failure_propagates(lake):
    """Parity with `weather`: an unwritable lake is a genuine capture failure,
    and the shared loop logs it and retries on the next tick."""
    mock_feed(full_league_csv())
    with pytest.raises(RuntimeError, match="lake unreachable"):
        await run_capture(SpyLake(fail_write=True))


@respx.mock
async def test_a_deadline_truncates_the_pass_rather_than_discarding_it(lake):
    mock_feed(full_league_csv())
    # Measured against the *real* clock, not `now`: the deadline exists to
    # bound wall-clock time, and `now` is a frozen instant the pass describes.
    result = await run_capture(
        lake, deadline=datetime.now(tz=UTC) - timedelta(seconds=1)
    )
    membership = result[MEMBERSHIP_SIGNAL]
    # The deadline is already past when the first team is reached, so every
    # slot is accounted for as truncated rather than silently absent.
    assert membership.coverage.present == 0
    assert len(membership.errors) == 416
    assert {e["reason"] for e in membership.errors} == {"deadline_exceeded"}
    assert len(lake.writes) == 2


@respx.mock
async def test_a_short_chart_is_visible_in_the_envelope(lake):
    mock_feed(
        depth_csv(
            [
                depth_row("KC", "WR", 1, "One Receiver"),
                depth_row("KC", "WR", 2, "Two Receiver"),
                depth_row("KC", "WR", 3, "Three Receiver"),
            ]
        )
    )
    result = await run_capture(lake)
    membership = result[MEMBERSHIP_SIGNAL]
    assert "KC:wr_depth_le_4:4" in membership.coverage.missing
    assert membership.coverage.present == 32 + 3


@respx.mock
async def test_capture_records_the_fleet_and_local_metrics(lake, metric_value, scrape):
    mock_feed(full_league_csv())
    before = (
        metric_value("scope_missed_producers_total", collector="roster-scope") or 0.0
    )
    await run_capture(lake)

    # One scrape, several lookups — see the `scrape` fixture: a second
    # collection reports every gauge as absent.
    read = scrape()
    assert (
        read(
            "collector_coverage_ratio",
            collector="roster-scope",
            signal_type=MEMBERSHIP_SIGNAL,
        )
        == 1.0
    )
    assert read("roster_scope_stale_depth_charts", collector="roster-scope") == 0.0
    # Recorded even though it is structurally zero today: an absent series and
    # a healthy one are indistinguishable in PromQL.
    after = read("scope_missed_producers_total", collector="roster-scope")
    assert after is not None
    assert after - before == 0.0


@respx.mock
async def test_stale_depth_charts_is_recorded_even_on_a_total_outage(lake, scrape):
    respx.get(FEED_2026).mock(return_value=httpx.Response(503))
    await run_capture(lake)
    assert scrape()("roster_scope_stale_depth_charts", collector="roster-scope") == 32.0


@respx.mock
async def test_missed_producers_is_recorded_on_the_ledger_failure_path(
    lake, metric_value
):
    """The one path that returns early. Without the explicit recording there,
    a lake outage would make the alertable series stop existing."""
    mock_feed(full_league_csv())
    before = (
        metric_value("scope_missed_producers_total", collector="roster-scope") or 0.0
    )
    await run_capture(SpyLake(fail_list=True))
    after = metric_value("scope_missed_producers_total", collector="roster-scope")
    assert after is not None
    assert after - before == 0.0

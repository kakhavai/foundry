"""Capture orchestration: versions, the outage paths, and the lake."""

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from collector_core.coverage import ERRORS_TRUNCATED, MAX_ERRORS

from roster_scope.capture import capture_scope
from roster_scope.matchups import MATCHUP_SIGNAL
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


class BlockingLake(SpyLake):
    """A lake whose every call blocks the calling thread for `delay` seconds.

    Stands in for an object store that is reachable but not answering —
    botocore's default connect timeout is 60s and it retries, so "slow" is
    minutes, not milliseconds.
    """

    def __init__(self, delay: float = 0.4) -> None:
        super().__init__()
        self.delay = delay

    def list_keys(self, *args, **kwargs):
        time.sleep(self.delay)
        return super().list_keys(*args, **kwargs)

    def write(self, envelope):
        time.sleep(self.delay)
        return super().write(envelope)


@respx.mock
async def test_capture_never_blocks_the_event_loop_on_the_lake():
    """The regression test for the failure that broke this service's first
    deploy.

    `capture_scope` is started as a task by `build_collector_app`'s lifespan.
    Its first statement used to be a *synchronous* boto3 ledger read, so the
    task ran to completion on the event loop before uvicorn could finish
    starting — `/health` never answered, readiness never passed, and
    `kubectl rollout status` timed out at 180s.

    This asserts the property directly rather than the symptom: while a
    capture against a slow lake is in flight, the event loop must still be
    servicing other coroutines. A heartbeat that cannot tick is a frozen
    process, whatever the HTTP layer happens to report.
    """
    mock_feed(full_league_csv())
    slow = BlockingLake(delay=0.4)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        await run_capture(slow)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat

    # Four blocking calls at 0.4s each (one list_keys, three writes) = ~1.6s
    # of blocking work. On the event loop the heartbeat would be starved
    # through all of it; off it, the loop keeps ticking throughout.
    assert ticks > 10, (
        f"the event loop only ticked {ticks} times during a capture against a "
        f"slow lake — the lake I/O is running on the event loop and will "
        f"freeze /health, exactly as it did on the first deploy"
    )
    assert len(slow.writes) == 3, "the capture must still have written"


@respx.mock
async def test_a_complete_capture_writes_all_three_envelopes(lake):
    mock_feed(full_league_csv())
    result = await run_capture(lake)

    assert set(result) == {MEMBERSHIP_SIGNAL, CHANGE_SIGNAL, MATCHUP_SIGNAL}
    membership = result[MEMBERSHIP_SIGNAL]
    assert membership.coverage.expected == 416
    assert membership.coverage.present == 416
    assert membership.coverage.ratio == 1.0
    assert membership.errors == []
    assert membership.scope == {"season": 2026, "week": 1, "scope_version": 1}
    assert len(lake.writes) == 3


@respx.mock
async def test_a_matchup_outage_does_not_mask_healthy_membership_coverage(lake):
    """M22: sharing one accumulator between the two envelopes would blend
    their coverage into a single number. `full_league_csv` fills every human
    and team-defense slot but carries none of the matchup positions
    (CB/S/LB/DL/OL) at all, so a correct implementation reports membership at
    100% while matchup — genuinely missing every one of its own 608 slots —
    reports far below it. If the two shared an accumulator, both numbers
    would move together instead."""
    mock_feed(full_league_csv())
    result = await run_capture(lake)

    membership = result[MEMBERSHIP_SIGNAL]
    matchup = result[MATCHUP_SIGNAL]
    assert membership.coverage.expected == 416
    assert membership.coverage.ratio == 1.0
    assert matchup.coverage.expected == 608
    assert matchup.coverage.present == 0
    assert matchup.coverage.ratio < 0.01


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
        return_value=httpx.Response(200, text=full_league_csv())
    )
    second = await run_capture(lake, week=2, now=NOW + timedelta(days=7))
    assert second[MEMBERSHIP_SIGNAL].scope["scope_version"] == 2


@respx.mock
async def test_a_rank_change_between_captures_emits_one_event(lake):
    """The chart flips KC's top two receivers; nothing else moves."""
    mock_feed(full_league_csv())
    await run_capture(lake)

    original = depth_csv(
        [
            depth_row("KC", "WR", 1, "KC WR Player1"),
            depth_row("KC", "WR", 2, "KC WR Player2"),
        ]
    ).split("\n", 1)[1]
    flipped = depth_csv(
        [
            depth_row("KC", "WR", 1, "KC WR Player2"),
            depth_row("KC", "WR", 2, "KC WR Player1"),
        ]
    ).split("\n", 1)[1]
    swapped = full_league_csv().replace(original, flipped)
    assert swapped != full_league_csv(), (
        "the rows this test means to swap were not found — the fixture's "
        "shape changed and this test would otherwise assert nothing"
    )
    respx.get(FEED_2026).mock(return_value=httpx.Response(200, text=swapped))
    second = await run_capture(lake, now=NOW + timedelta(hours=1))

    events = second[CHANGE_SIGNAL].signals
    assert {e["transition"] for e in events} == {"rank_changed"}
    assert len(events) == 2
    assert second[MEMBERSHIP_SIGNAL].coverage.present == 416


@respx.mock
async def test_a_total_upstream_outage_still_writes_an_envelope(lake):
    """A gap in an append-only lake must be explicit, never inferred from
    absence. This collector degrades in place rather than raising; `weather`
    raises but now writes a failure envelope first
    (`collector_core.failure.fail_capture`). Two shapes, same guarantee."""
    respx.get(FEED_2026).mock(return_value=httpx.Response(503))
    result = await run_capture(lake)

    membership = result[MEMBERSHIP_SIGNAL]
    assert membership.coverage.expected == 416
    assert membership.coverage.present == 32
    assert round(membership.coverage.ratio, 3) == 0.077
    assert len(lake.writes) == 3

    reasons = {e["reason"] for e in membership.errors}
    assert "http_status" in reasons, "the fetch failure itself is classified"
    assert "depth_chart_unavailable" in reasons
    assert any(e.get("detail") == "depth_chart_fetch" for e in membership.errors)

    # The same fetch feeds both envelopes, so its failure is recorded
    # independently in the matchup envelope's own errors too -- not by
    # sharing membership's accumulator, but via its own `add_error` call.
    matchup = result[MATCHUP_SIGNAL]
    assert matchup.coverage.expected == 608
    assert matchup.coverage.present == 0
    assert any(e.get("detail") == "depth_chart_fetch" for e in matchup.errors)


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

    matchup = result[MATCHUP_SIGNAL]
    assert matchup.coverage.expected == 608
    assert matchup.coverage.present == 0
    assert len(matchup.coverage.missing) == 608

    # It still writes: the failure is recorded in the lake, not inferred from
    # a hole in it.
    assert len(broken.writes) == 3


@respx.mock
async def test_a_ledger_failure_does_not_fetch_the_upstream(lake):
    """No version can be minted, so hitting the upstream would spend a call
    on a pass that cannot produce anything."""
    route = mock_feed(full_league_csv())
    await run_capture(SpyLake(fail_list=True))
    assert route.call_count == 0


@respx.mock
async def test_a_lake_write_failure_does_not_cost_the_resolved_scope(lake):
    """Parity with `weather`, whose test moved the same way. An unwritable
    lake used to be treated as a genuine capture failure and re-raised, which
    is wrong when the depth-chart fetch and the whole resolution succeeded:
    every other collector fetches this membership list before pulling
    anything, so dropping it on an object-store outage takes the fleet's
    denominator with it. `collector_core.publish.publish_capture` counts the
    failure and returns the envelopes."""
    mock_feed(full_league_csv())

    result = await run_capture(SpyLake(fail_write=True))

    assert set(result) == {MEMBERSHIP_SIGNAL, CHANGE_SIGNAL, MATCHUP_SIGNAL}
    assert len(result) == 3
    assert result[MEMBERSHIP_SIGNAL].coverage.present > 0


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
    # Capped, with the drop stated rather than silent: all 416 slots are
    # accounted for in `coverage.missing`, but 416 near-identical error
    # entries in every envelope and every lake object is the payload bloat
    # `collector_core.coverage.cap_errors` exists to bound.
    assert len(membership.errors) == MAX_ERRORS + 1
    assert {e["reason"] for e in membership.errors[:MAX_ERRORS]} == {
        "deadline_exceeded"
    }
    assert membership.errors[-1]["reason"] == ERRORS_TRUNCATED
    assert membership.errors[-1]["total"] == 416
    assert len(membership.coverage.missing) == 416
    assert len(lake.writes) == 3


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

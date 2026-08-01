"""The unchanged-snapshot gate, and its durability half.

A `weekly` collector re-reads every 24 hours while the broadcast schedule
changes a handful of times a season. Publishing a byte-identical envelope on
every pass would fill an append-only lake with objects carrying no
information — and it would make `history.read_history` proportional to how
often we looked rather than to how often anything happened.

The failure that put `PublishResult.landed` in the library takes **two
passes** to see, so every test here runs two.
"""

import pytest
from collector_core.conditional import UpstreamUnchanged

from broadcast_context.capture import SIGNAL, reset_published_digests

from .conftest import NOW, SpyLake, feed_document, run_capture, week_rows

LATER = NOW.replace(day=16)


# Every capture-scope-sensitive test below runs at week 1 AND week 5. Both
# the digest key and the history partition are keyed by `(season, week)`, and
# a literal `1` in either place passed the whole suite — the exact shape
# `officiating` shipped, where a digest read keyed to week 1 was green
# everywhere. Nothing about this collector is week-1 specific; the fixture
# just happened to be.
CAPTURE_WEEKS = (1, 5)


@pytest.mark.parametrize("week", CAPTURE_WEEKS)
async def test_an_identical_second_pass_is_reported_unchanged(week):
    """`run_capture_loop` and `_run_capture` treat `UpstreamUnchanged` as a
    successful pass: `last_capture_at` advances and
    `collector_upstream_unchanged_total` increments, while `/signals` keeps
    serving the same rows."""
    lake = SpyLake()
    document = feed_document(week_rows(1))

    await run_capture(document, lake=lake, week=week)
    assert len(lake.writes) == 1

    with pytest.raises(UpstreamUnchanged):
        await run_capture(document, lake=lake, week=week, now=LATER)
    assert len(lake.writes) == 1, "an unchanged pass must not append"


@pytest.mark.parametrize("week", CAPTURE_WEEKS)
async def test_a_changed_slate_publishes_again(week):
    """The other arm. Without it, a gate that raised unconditionally would
    pass every assertion above."""
    lake = SpyLake()
    await run_capture(feed_document(week_rows(1)), lake=lake, week=week)

    moved = week_rows(1)
    # Move the last early-window game to Sunday night: a real flex.
    moved[0] = moved[0].replace(",13:00,", ",20:20,")
    await run_capture(feed_document(moved), lake=lake, week=week, now=LATER)

    assert len(lake.writes) == 2
    flexed = {
        row["game_id"]: row
        for row in lake.writes[1].signals
        if row["flex_status"] != "original"
    }
    assert list(flexed) == ["2026_01_A00_B00"]
    assert flexed["2026_01_A00_B00"]["flex_status"] == "flexed_in"
    assert flexed["2026_01_A00_B00"]["previous_window_id"] == "sun_early"
    assert flexed["2026_01_A00_B00"]["observed_window_count"] == 2
    assert flexed["2026_01_A00_B00"]["first_observed_at"] == "2026-09-16T12:00:00Z"


async def test_the_digest_table_is_keyed_by_the_capture_week():
    """Two partitions, one process, identical content.

    A digest key with a literal week would match the first partition's entry
    and raise `UpstreamUnchanged` for the second — so the second partition
    would never receive its first object at all. Both passes use the same
    `now` deliberately, so the rows really are byte-identical and only the key
    can tell them apart.
    """
    lake = SpyLake()
    document = feed_document(week_rows(1))

    await run_capture(document, lake=lake, week=1)
    await run_capture(document, lake=lake, week=5)

    assert len(lake.writes) == 2
    assert {envelope.scope["week"] for envelope in lake.writes} == {1, 5}


async def test_a_capture_reads_history_from_its_own_partition():
    """`read_history`'s own partition test calls it directly, so the
    `week=week` wiring inside `capture_broadcast_context` is unproven by it.

    Week 1 gets a real history; week 5's partition is empty, so every game
    there must read `original`. A literal week in the history read would let
    week 1's snapshot describe week 5's capture and report a flex that this
    partition has no evidence for.
    """
    lake = SpyLake()
    await run_capture(feed_document(week_rows(1)), lake=lake, week=1)

    moved = week_rows(1)
    moved[0] = moved[0].replace(",13:00,", ",20:20,")
    envelopes = await run_capture(feed_document(moved), lake=lake, week=5, now=LATER)

    rows = envelopes[SIGNAL].signals
    assert len(rows) == 16
    assert {row["flex_status"] for row in rows} == {"original"}
    assert {row["observed_window_count"] for row in rows} == {1}


async def test_a_time_change_is_followed_across_a_third_pass():
    """Three passes, because a kickoff-only move looks correct on the second.

    Deduping the history chain on `window_id` alone instead of the whole
    broadcast state passes every two-pass test: pass 2 still classifies the
    move correctly. It breaks on pass 3, where pass 2's state was never
    recorded — so the chain still holds only the ORIGINAL kickoff, the state
    reads as changed again, and `first_observed_at` restamps to `now` on every
    pass forever. That is the retroactive-certainty failure by the back door,
    plus a digest that changes every pass and fills an append-only lake with
    daily objects.

    The third pass asserting `UpstreamUnchanged` is what catches it.
    """
    lake = SpyLake()
    original = week_rows(1)
    await run_capture(feed_document(original), lake=lake)

    # 13:00 -> 15:00 on the same Sunday: the kickoff moved, the window
    # (`sun_early`, anything before 16:00) did not.
    moved = list(original)
    moved[0] = moved[0].replace(",13:00,", ",15:00,")
    second = await run_capture(feed_document(moved), lake=lake, now=LATER)

    row = next(r for r in second[SIGNAL].signals if r["game_id"] == "2026_01_A00_B00")
    assert row["flex_status"] == "time_changed"
    assert row["window_id"] == "sun_early"
    assert row["previous_window_id"] == "sun_early"
    assert row["observed_window_count"] == 2
    assert row["first_observed_at"] == "2026-09-16T12:00:00Z"
    assert len(lake.writes) == 2

    # Pass three, unchanged. The chain must already hold the moved state.
    with pytest.raises(UpstreamUnchanged):
        await run_capture(feed_document(moved), lake=lake, now=NOW.replace(day=17))
    assert len(lake.writes) == 2


async def test_a_failed_write_still_serves_the_capture():
    """Availability wins over durability. The capture succeeded; only its
    archival copy did not, and `/signals` must not lose a capture that is
    already built and correct."""
    broken = SpyLake(fail_write=True)
    envelopes = await run_capture(feed_document(week_rows(1)), lake=broken)
    assert len(envelopes[SIGNAL].signals) == 16
    assert broken.writes == []


async def test_a_digest_is_not_recorded_for_a_write_that_never_landed():
    """The failure `PublishResult.landed` exists for, reproduced.

    **Three passes, and the restart is load-bearing.** The obvious two-pass
    version — fail the write, then retry against a healthy lake — cannot fire
    on this collector, and it took a mutation to notice: a state the lake has
    never seen carries `first_observed_at: now`, so the retry's rows differ
    from the failed pass's rows and the digest could not have matched anyway.
    That version passes with `landed` replaced by `True`.

    What actually reaches the gate is the pairing this test builds: a lake
    that already holds the state (so `first_observed_at` is read back rather
    than restamped) plus an empty in-process digest table (so the gate has
    nothing recorded). That is a **pod restart** — the digests are per process
    and the lake is not — followed by an object-store blip. Then two
    consecutive passes really do produce byte-identical rows, and recording a
    digest for the write that failed suppresses the retry until the broadcast
    schedule itself changes. On a weekly cadence that is months.
    """
    document = feed_document(week_rows(1))
    lake = SpyLake()

    # A healthy pass, so the lake holds this state and `first_observed_at`
    # stops moving.
    await run_capture(document, lake=lake)
    assert len(lake.writes) == 1

    # The restart: the digest table is per process, the lake is not.
    reset_published_digests()

    # The blip. Same rows as the pass before it, and the write fails.
    lake.fail_write = True
    envelopes = await run_capture(document, lake=lake, now=LATER)
    assert len(envelopes[SIGNAL].signals) == 16, "the capture itself succeeded"
    assert len(lake.writes) == 1, "nothing landed"

    # The retry, with the object store back. Byte-identical content.
    lake.fail_write = False
    await run_capture(document, lake=lake, now=LATER.replace(day=17))
    assert len(lake.writes) == 2, (
        "the retry was suppressed — a digest was recorded for a write the "
        "lake never received"
    )


async def test_an_unchanged_pass_leaves_first_observed_at_alone():
    """The digest can only be stable if `first_observed_at` is, so this is the
    same guarantee seen from the other side: a pass that changes nothing must
    read the instant back out of the lake rather than restamping it."""
    lake = SpyLake()
    first = await run_capture(feed_document(week_rows(1)), lake=lake)
    stamped = {
        row["game_id"]: row["first_observed_at"] for row in first[SIGNAL].signals
    }
    assert set(stamped.values()) == {"2026-09-15T12:00:00Z"}

    with pytest.raises(UpstreamUnchanged):
        await run_capture(feed_document(week_rows(1)), lake=lake, now=LATER)


async def test_a_304_is_a_successful_pass_and_writes_nothing():
    """Conditional GET: nflverse answers `If-None-Match` with a `304` carrying
    zero bytes. Routed into `fail_capture` it would write a `present: 0`
    envelope over a healthy capture and count a failure that did not happen,
    so it is re-raised above the generic handler."""
    lake = SpyLake()
    await run_capture(feed_document(week_rows(1)), lake=lake, headers={"ETag": '"v1"'})
    assert len(lake.writes) == 1

    with pytest.raises(UpstreamUnchanged):
        await run_capture(None, lake=lake, now=LATER, status=304)
    assert len(lake.writes) == 1

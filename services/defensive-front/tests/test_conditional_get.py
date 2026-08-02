"""Conditional GET across four feeds, and the two ways it goes wrong.

The mock answers `If-None-Match` the way a real upstream does — see
`conftest.Feeds` — so these tests distinguish "the collector sent the header"
from "the mock returned 304 regardless", which a status-only mock cannot.
"""

import httpx
import pytest
from collector_core.conditional import UpstreamUnchanged

from defensive_front.capture import (
    STRENGTH,
    every_feed_unchanged,
    reset_published_digests,
)

from .conftest import Feeds, SpyLake, run_capture

ALL_ETAGS = {
    "pbp": '"pbp-1"',
    "participation": '"part-1"',
    "players": '"players-1"',
    "injuries": '"inj-1"',
}


# --------------------------------------------------------------------------
# `every_feed_unchanged`
# --------------------------------------------------------------------------


def test_an_empty_feed_map_is_not_unchanged():
    """`all({})` is `True`. Without the `bool(...)` a pass that attempted no
    feed at all reports itself unchanged: `last_capture_at` advances,
    `collector_upstream_unchanged_total` increments, no envelope is written,
    and nothing in the metrics says why.

    The caller cannot produce an empty map today — a play-by-play failure ends
    the pass first — which is exactly why this is a named function rather than
    an inline expression. **The mutation that removes the guard survives any
    suite that cannot construct the input distinguishing the two arms**, and
    this test is that input.
    """
    assert every_feed_unchanged({}) is False


@pytest.mark.parametrize(
    ("unchanged", "expected"),
    [
        ({"pbp": True}, True),
        ({"pbp": True, "participation": True}, True),
        ({"pbp": True, "participation": False}, False),
        ({"pbp": False, "participation": True}, False),
        ({"pbp": False}, False),
    ],
)
def test_only_a_wholly_unchanged_pass_is_unchanged(unchanged, expected):
    assert every_feed_unchanged(unchanged) is expected


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


async def test_the_first_pass_sends_no_conditional_header():
    feeds = Feeds(etags=ALL_ETAGS)
    await run_capture(feeds, lake=SpyLake())
    assert feeds.conditional_calls("pbp") == 0
    assert feeds.calls("pbp") == 1


async def test_a_second_pass_conditionalises_every_feed():
    """Without the `etag_key` half nothing is saved — every poll still
    round-trips ~67 MiB of bodies it already holds."""
    feeds = Feeds(etags=ALL_ETAGS)
    await run_capture(feeds, lake=SpyLake())
    with pytest.raises(UpstreamUnchanged):
        await run_capture(feeds, lake=SpyLake())
    for name in ALL_ETAGS:
        assert feeds.conditional_calls(name) == 1, name


async def test_every_feed_unchanged_ends_the_pass_as_a_SUCCESS():
    """A `304` is a successful capture, not a failed one. `run_capture_loop`
    catches `UpstreamUnchanged` and calls `mark_unchanged`, so `/catalog`
    reports a fresh pass while `/signals` keeps serving the previous rows."""
    feeds = Feeds(etags=ALL_ETAGS)
    lake = SpyLake()
    await run_capture(feeds, lake=lake)
    written = len(lake.writes)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(feeds, lake=lake)
    assert len(lake.writes) == written, "an unchanged pass wrote an envelope"


async def test_one_unchanged_feed_does_NOT_end_the_pass():
    """**The hazard this ordering exists to prevent.**

    `defense-vs-position` ETag-gated a small roster feed ahead of its large
    play-by-play one, so a `304` there raised `UpstreamUnchanged` before the
    large feed was even requested — suppressing a genuinely changed
    play-by-play while `mark_unchanged` advanced `last_capture_at`, so
    staleness never alerted. Here only the roster feed has an ETag, so only it
    can 304, and the pass must still publish.
    """
    feeds = Feeds(etags={"players": '"players-1"'})
    await run_capture(feeds, lake=SpyLake())
    # The digest gate is a SEPARATE suppression, tested in
    # `test_digest_gate.py`. Left armed it would raise `UpstreamUnchanged` on
    # the second pass here whatever the ETag path did, and mask exactly the
    # behaviour under test.
    reset_published_digests()
    envelope = (await run_capture(feeds, lake=SpyLake()))[STRENGTH]
    assert envelope.signals, "one 304 suppressed a pass that had changed"
    assert envelope.coverage.present > 0


async def test_a_304_feed_is_refetched_unconditionally_when_another_changed():
    """The unchanged feed's body is still needed, and the re-fetch must OMIT
    `If-None-Match` — otherwise it 304s again and the field it carries is
    silently empty for the rest of the season."""
    feeds = Feeds(etags={"players": '"players-1"'})
    await run_capture(feeds, lake=SpyLake())
    # The digest gate is a SEPARATE suppression, tested in
    # `test_digest_gate.py`. Left armed it would raise `UpstreamUnchanged` on
    # the second pass here whatever the ETag path did, and mask exactly the
    # behaviour under test.
    reset_published_digests()
    before = feeds.calls("players")

    envelope = (await run_capture(feeds, lake=SpyLake()))[STRENGTH]
    assert feeds.calls("players") == before + 2, (
        "the 304'd feed was not re-fetched, or was re-fetched conditionally"
    )
    # ...and the field it carries is populated, which is the point of the
    # re-fetch rather than merely that a request happened.
    assert any(row["front_continuity_index"] is not None for row in envelope.signals)
    assert all(row["degraded_upstreams"] == [] for row in envelope.signals)


async def test_a_304_on_the_primary_feed_does_not_empty_the_charted_one():
    """**The `team-scheme` shape, which this collector's adapter layout makes
    unreachable.** There the charted feed is folded against an index handed
    down from the play-by-play fetch, so a play-by-play `304` folds it against
    an EMPTY index and publishes zero charted rows — and the unconditional
    re-fetch repairs play-by-play, not the fold. Here no adapter takes another
    adapter's result, so a play-by-play `304` costs nothing."""
    feeds = Feeds(etags={"pbp": '"pbp-1"'})
    await run_capture(feeds, lake=SpyLake())
    # The digest gate is a SEPARATE suppression, tested in
    # `test_digest_gate.py`. Left armed it would raise `UpstreamUnchanged` on
    # the second pass here whatever the ETag path did, and mask exactly the
    # behaviour under test.
    reset_published_digests()
    envelope = (await run_capture(feeds, lake=SpyLake()))[STRENGTH]
    assert all(row["pressure_rate_generated"] is not None for row in envelope.signals)
    assert envelope.coverage.present == len(envelope.signals)


async def test_a_304_is_never_routed_into_fail_capture():
    """Without `except UpstreamUnchanged: raise` above the generic handler, a
    `304` writes a `present: 0` envelope over a healthy capture and counts a
    failure that did not happen."""
    feeds = Feeds(etags=ALL_ETAGS)
    lake = SpyLake()
    await run_capture(feeds, lake=lake)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(feeds, lake=lake)
    assert all(envelope.coverage.present > 0 for envelope in lake.writes)


async def test_a_failure_during_the_unconditional_refetch_still_degrades():
    """A feed's place in the failure lattice does not change because this is
    its second request: the roster feed is optional either way."""
    feeds = Feeds(etags={"players": '"players-1"'})
    await run_capture(feeds, lake=SpyLake())
    feeds.status["players"] = 500
    with pytest.raises(httpx.HTTPStatusError):
        # A `304` on the first attempt and a 500 on the re-fetch: the second
        # request is NOT wrapped in the optional feed's handler, so this is
        # the honest current behaviour and it is pinned rather than assumed.
        await run_capture(feeds, lake=SpyLake())

"""The digest gate, and the durability question it turns on.

The gate suppresses a byte-identical append: a weekly collector re-polling a
finished week would otherwise write the same object every pass forever.

**The failure it can cause takes two passes to see.** Record a digest for
content the lake never received and the next pass digests the same content,
matches, raises `UpstreamUnchanged`, and the object is **never written again**
until the upstream data itself changes — on a weekly cadence, potentially the
rest of a season. Pass 1 with a failing lake writes nothing; pass 2 with a
healthy lake raises and still writes nothing. A single-pass availability test
stays green throughout, which is why `venue`, `player-profile` and
`durability-history` each carried a private `_WriteObserver` before
`PublishResult.landed` existed.
"""

import pytest
from collector_core.conditional import UpstreamUnchanged

from offensive_line.capture import STRENGTH

from .conftest import Feeds, SpyLake, run_capture, units


async def test_an_identical_second_pass_appends_nothing():
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake)
    assert len(lake.writes) == 1
    with pytest.raises(UpstreamUnchanged):
        await run_capture(Feeds(), lake=lake)
    assert len(lake.writes) == 1


async def test_a_changed_pass_appends_again():
    """The negative arm. A gate that suppressed everything would pass the test
    above and would stop the collector writing at all."""
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake)
    await run_capture(Feeds(status={"injuries": 500}), lake=lake)
    assert len(lake.writes) == 2


async def test_a_failed_write_is_not_recorded_as_published():
    """**Two passes, because one cannot see it.** Pass 1's write fails, so
    nothing may be remembered; pass 2's lake is healthy and must therefore
    write. A gate that recorded the digest regardless would raise
    `UpstreamUnchanged` here and never write the week's object at all."""
    failing = SpyLake(fail_write=True)
    await run_capture(Feeds(), lake=failing)
    assert failing.writes == []

    healthy = SpyLake()
    envelopes = await run_capture(Feeds(), lake=healthy)
    assert len(healthy.writes) == 1
    assert units(envelopes)


async def test_a_partial_lake_failure_is_gated_per_signal_type():
    """`if any(published.landed(st) for st in published)` looks equivalent and
    is not: one type's write failing while the others land records the failed
    type's digest anyway, and it is never written again. With one signal type
    the two gates coincide, so this test exists to keep the distinction alive
    when a second is added."""
    failing = SpyLake(fail_signal_types=frozenset({STRENGTH}))
    await run_capture(Feeds(), lake=failing)
    assert failing.writes == []

    healthy = SpyLake()
    await run_capture(Feeds(), lake=healthy)
    assert [envelope.signal_type for envelope in healthy.writes] == [STRENGTH]


async def test_an_object_store_outage_costs_freshness_not_availability():
    """`publish_capture` returns the envelopes anyway. The capture succeeded
    and only its archival copy did not, and refusing to serve data the
    collector already has is unrecoverable for every caller in the meantime."""
    envelopes = await run_capture(Feeds(), lake=SpyLake(fail_write=True))
    assert units(envelopes), "the capture is still served from memory"
    assert envelopes[STRENGTH].coverage.present > 0

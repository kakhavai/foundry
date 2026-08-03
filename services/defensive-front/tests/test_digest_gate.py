"""The digest gate, and the durability failure that makes it permanent.

The gate suppresses a byte-identical append. Its failure mode is the sharpest
one in the fleet: **a digest recorded for content the lake never received is
never written again until the upstream data itself changes.** The next pass
digests the same content, matches, raises `UpstreamUnchanged`, writes nothing.
On this collector's weekly cadence that is potentially the rest of a season.

It takes **two passes** to see. The single-pass availability test — "a lake
outage still returns the envelopes" — stays green throughout.
"""

import pytest
from collector_core.conditional import UpstreamUnchanged

from defensive_front.capture import STRENGTH, reset_published_digests

from .conftest import Feeds, SpyLake, run_capture

# **Not week 1.** A gate keyed on `(season, signal_type)` and not on the week
# passes every single-week test, and then suppresses every week after the
# first for a whole season. Two distinct weeks are exercised below, live in
# one process, for the reason in `test_two_weeks_are_gated_independently`.
WEEK_A = 5
WEEK_B = 9


async def test_an_unchanged_pass_is_not_appended_again():
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake, week=WEEK_A)
    assert len(lake.writes) == 1
    with pytest.raises(UpstreamUnchanged):
        await run_capture(Feeds(), lake=lake, week=WEEK_A)
    assert len(lake.writes) == 1


async def test_a_changed_pass_is_appended():
    """The negative arm. A gate that suppressed everything would pass the test
    above."""
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake, week=WEEK_A)
    await run_capture(Feeds(defense_release_shift=2.0), lake=lake, week=WEEK_A)
    assert len(lake.writes) == 2


async def test_two_weeks_are_gated_independently():
    """**Two distinct keys live in one process**, which is the only shape that
    catches the symmetric mutant.

    Parameterising a test over the scoping key catches the *asymmetric* one —
    a gate that ignores the week entirely. It does not catch a gate that uses
    a wrong-but-consistent key, because each parameter run starts with an
    empty dict and every key looks fresh. Both weeks have to be gated inside
    one process, against one dict, for the difference to exist.
    """
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake, week=WEEK_A)
    # Same content, different week: not a duplicate, and must be appended.
    await run_capture(Feeds(), lake=lake, week=WEEK_B)
    assert len(lake.writes) == 2
    assert {envelope.scope["week"] for envelope in lake.writes} == {WEEK_A, WEEK_B}

    # ...and each week is now independently suppressed.
    with pytest.raises(UpstreamUnchanged):
        await run_capture(Feeds(), lake=lake, week=WEEK_A)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(Feeds(), lake=lake, week=WEEK_B)
    assert len(lake.writes) == 2


async def test_a_lake_outage_still_returns_the_envelopes():
    """Availability wins over durability. The capture succeeded and only its
    archival copy did not; refusing to serve data the collector already has is
    the inversion the cache exists to prevent."""
    envelopes = await run_capture(Feeds(), lake=SpyLake(fail_write=True), week=WEEK_A)
    assert envelopes[STRENGTH].signals


async def test_a_digest_is_not_recorded_for_a_write_that_did_not_land():
    """**The two-pass bug.** Pass 1 against a failing lake must NOT record its
    digest; pass 2 against a healthy one must therefore write.

    Reproduced on `venue`: pass 1 wrote nothing, pass 2 raised
    `UpstreamUnchanged` and still wrote nothing, and the object was absent
    from the lake until the upstream data itself changed.
    """
    await run_capture(Feeds(), lake=SpyLake(fail_write=True), week=WEEK_A)

    healthy = SpyLake()
    envelopes = await run_capture(Feeds(), lake=healthy, week=WEEK_A)
    assert len(healthy.writes) == 1, (
        "the failed pass recorded a digest, so the retry was suppressed permanently"
    )
    assert envelopes[STRENGTH].signals


async def test_the_gate_is_keyed_per_signal_type_not_per_pass():
    """`if any(published.landed(st) for st in published)` looks equivalent and
    is not: one type's write failing while the others land records the failed
    type's digest anyway, and it is never written again.

    With one signal type today the two readings coincide, so this test pins
    the *mechanism* — a per-signal-type failure suppresses only that type's
    digest — against the day a second one is added.
    """
    failing = SpyLake(fail_signal_types=frozenset({STRENGTH}))
    await run_capture(Feeds(), lake=failing, week=WEEK_A)
    assert failing.writes == []

    healthy = SpyLake()
    await run_capture(Feeds(), lake=healthy, week=WEEK_A)
    assert [envelope.signal_type for envelope in healthy.writes] == [STRENGTH]


async def test_the_digest_survives_a_new_capture_of_identical_content():
    """`hashlib`, never `hash()`. Python salts `hash()` on `str` per process,
    so a digest built from it would differ between two pods and between one
    pod's restarts — every pass would look changed and the gate would silently
    do nothing. Proved by the suppression above holding across two separately
    built `Feeds` objects, which is two independent constructions of the same
    content.
    """
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake, week=WEEK_A)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(Feeds(), lake=lake, week=WEEK_A)


async def test_resetting_the_digests_re_arms_the_gate():
    """The helper the whole suite depends on. If it silently did nothing,
    every test that resets would be testing a stale process state."""
    lake = SpyLake()
    await run_capture(Feeds(), lake=lake, week=WEEK_A)
    reset_published_digests()
    await run_capture(Feeds(), lake=lake, week=WEEK_A)
    assert len(lake.writes) == 2

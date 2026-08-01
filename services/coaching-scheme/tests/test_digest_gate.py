"""The unchanged-snapshot gate, conditional GET, and the durability gate.

A `seasonal` collector re-reads every 24 hours while this data changes once a
week. Three mechanisms keep that from filling an append-only lake with
identical objects, and each fails silently in its own way:

* **conditional GET** — bandwidth. A `304` is a *successful* capture.
* **the digest gate** — lake objects. Per signal type, keyed by
  `(season, week, signal_type)`.
* **`PublishResult.landed`** — the durability gate. A digest recorded for a
  write that never landed permanently suppresses the retry.
"""

import pytest
from collector_core.conditional import ETAGS, UpstreamUnchanged

from coaching_scheme.adapters import pbp as pbp_adapter
from coaching_scheme.capture import PROFILE, STAFF, every_feed_unchanged

from .conftest import (
    LATER,
    SEASON,
    Feeds,
    SpyLake,
    coaches_with_change,
    proe_with_shift,
    run_capture,
)

# Every scope-sensitive test below runs at week 1 AND week 5. Both the digest
# key and the lake partition are keyed by `(season, week)`, and a literal week
# in either is a bug `officiating` actually shipped. Nothing about this
# collector is week-1 specific.
CAPTURE_WEEKS = (1, 5)

# One ETag per feed, so a second pass with the same content really 304s.
ALL_V1 = {
    "games": '"v1"',
    "pbp": '"v1"',
    "ftn": '"v1"',
    "participation": '"v1"',
}


@pytest.mark.parametrize("week", CAPTURE_WEEKS)
async def test_an_identical_second_pass_is_reported_unchanged(week, lake: SpyLake):
    """Byte-identical rows carry no information, so nothing is appended."""
    await run_capture(lake=lake, week=week)
    written = len(lake.writes)
    assert written == 2

    with pytest.raises(UpstreamUnchanged):
        await run_capture(lake=lake, week=week, now=LATER)
    assert len(lake.writes) == written


@pytest.mark.parametrize("week", CAPTURE_WEEKS)
async def test_a_changed_staff_feed_publishes_again(week, lake: SpyLake):
    await run_capture(lake=lake, week=week)
    await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7)),
        lake=lake,
        week=week,
        now=LATER,
    )
    assert len(lake.writes) == 4


async def test_the_two_signal_types_are_gated_independently(lake: SpyLake):
    """PER SIGNAL TYPE, not per pass.

    A rate change with no staff change must append the profile envelope and
    **not** the staff one. One digest spanning both would re-append a
    byte-identical staff envelope after every week's games — all 32 teams,
    every week of the season.
    """
    await run_capture(lake=lake)
    assert {envelope.signal_type for envelope in lake.writes} == {STAFF, PROFILE}

    await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=20.0)),
        lake=lake,
        now=LATER,
    )
    assert [envelope.signal_type for envelope in lake.writes[2:]] == [PROFILE]


async def test_the_digest_table_is_keyed_by_the_capture_week():
    """Two partitions, one process, identical content.

    **This is the sole killer of the symmetric hardcoding mutant.**
    Parameterising a test over the scoping key (as several above do) catches
    only the *asymmetric* half — where the read and the write disagree, so it
    misbehaves at any single key. The symmetric half, both sides keyed to a
    literal, behaves consistently at every key and passes every parameterised
    run. Catching it needs two distinct keys **live in one process** with
    content the digest cannot tell apart, which is why both passes here share
    one `now`.
    """
    lake = SpyLake()
    await run_capture(lake=lake, week=1)
    await run_capture(lake=lake, week=5)

    assert len(lake.writes) == 4
    assert {envelope.scope["week"] for envelope in lake.writes} == {1, 5}


# --------------------------------------------------------------------------
# The durability gate
# --------------------------------------------------------------------------


async def test_a_failed_write_still_serves_the_capture():
    """Availability over durability: the capture succeeded and only its
    archival copy did not, so the envelopes come back anyway."""
    broken = SpyLake(fail_write=True)
    envelopes = await run_capture(lake=broken)
    assert set(envelopes) == {STAFF, PROFILE}
    assert broken.writes == []


async def test_a_digest_is_not_recorded_for_a_write_that_never_landed():
    """**The permanent-suppression failure.**

    Pass 1 against a dead lake builds correct envelopes and writes nothing.
    Pass 2 against a healthy lake must write them. Record the digest without
    checking `landed` and pass 2 raises `UpstreamUnchanged` instead, and the
    object is never written until the upstream itself changes — on a seasonal
    cadence, a week at best.
    """
    broken = SpyLake(fail_write=True)
    await run_capture(lake=broken)

    healthy = SpyLake()
    await run_capture(lake=healthy, now=LATER)
    assert {envelope.signal_type for envelope in healthy.writes} == {STAFF, PROFILE}


async def test_a_landed_write_does_record_its_digest(lake: SpyLake):
    """The other side. Without it, `landed` could return a constant `False`
    and the gate would be off entirely while the test above still passed."""
    await run_capture(lake=lake)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(lake=lake, now=LATER)


# --------------------------------------------------------------------------
# Conditional GET across four feeds
# --------------------------------------------------------------------------


async def test_all_four_feeds_unchanged_is_a_successful_unchanged_pass(
    lake: SpyLake,
):
    """A 304 everywhere means the pass is unchanged — no envelope, no
    failure. Routing it into `fail_capture` would write `present: 0` over a
    healthy capture."""
    await run_capture(Feeds(etags=ALL_V1), lake=lake)
    assert len(lake.writes) == 2

    second = Feeds(etags=ALL_V1)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(second, lake=lake, now=LATER)
    # Every feed was asked conditionally, and every one answered 304. Asserted
    # on the requests rather than on the raise: a collector that sent no
    # `If-None-Match` at all would never reach this branch, and the raise
    # alone cannot tell that apart from one that did.
    assert {name for name, _ in second.requests} == set(ALL_V1)
    assert all(sent == '"v1"' for _, sent in second.requests), second.requests


async def test_one_changed_feed_forces_the_others_to_be_re_read(lake: SpyLake):
    """**The all-or-nothing trap, and the reason this collector does not take
    it.**

    `games.csv` is a live master file; the three release assets change once a
    week. So the common mixed case is exactly this one — and re-raising the
    first `UpstreamUnchanged` (which `officiating` does) would discard a
    mid-season coaching change, the one event that lands off-cycle.

    Pass 1 caches every ETag. Pass 2 serves a *changed* games.csv and 304s the
    other three, and must still publish the new revision.
    """
    await run_capture(Feeds(etags=ALL_V1), lake=lake)

    # games.csv republished under a new ETag; the other three are byte
    # identical and still on "v1", so they really do answer 304.
    second = Feeds(
        coaches=coaches_with_change("AAA", at_week=7),
        etags={**ALL_V1, "games": '"v2"'},
    )
    envelopes = await run_capture(second, lake=lake, now=LATER)

    # Each 304'd feed was asked twice: once conditionally, then again with no
    # `If-None-Match` because another feed changed.
    assert second.requests.count(("pbp", '"v1"')) == 1
    assert second.requests.count(("pbp", None)) == 1

    revisions = {
        row["revision_id"]
        for row in envelopes[STAFF].signals
        if row["team_id"] == "AAA"
    }
    assert len(revisions) == 2, revisions
    # And the re-read feeds really produced rates, rather than the profile
    # silently degrading to nulls.
    profile_rows = [
        row for row in envelopes[PROFILE].signals if row["team_id"] == "AAA"
    ]
    assert profile_rows
    assert all(row["pass_rate_over_expected"] is not None for row in profile_rows)


async def test_the_shared_etag_store_survives_an_unconditional_re_read(
    lake: SpyLake,
):
    """The re-fetch uses a throwaway store, so the shared one keeps its
    (still-correct) entry and the next pass 304s again normally. Committing
    the re-read's response into the shared store would be harmless here and
    wrong in general — it would overwrite a valid ETag with one obtained
    without `If-None-Match`."""
    await run_capture(Feeds(etags=ALL_V1), lake=lake)
    before = ETAGS.get(pbp_adapter.source_ref(SEASON))
    assert before == '"v1"'

    await run_capture(
        Feeds(
            coaches=coaches_with_change("AAA", at_week=7),
            etags={**ALL_V1, "games": '"v2"'},
        ),
        lake=lake,
        now=LATER,
    )
    assert ETAGS.get(pbp_adapter.source_ref(SEASON)) == before


# --------------------------------------------------------------------------
# The `all({})` guard
# --------------------------------------------------------------------------


def test_no_feed_attempted_is_not_an_unchanged_pass():
    """`all({})` is `True`, and that would report a pass that fetched nothing
    as byte-identical to the last one — `last_capture_at` advancing,
    `collector_upstream_unchanged_total` incrementing, no envelope written,
    and nothing to say why.

    The caller cannot currently produce an empty map: a `games.csv` failure
    ends the pass before the check is reached. That is exactly why the
    predicate is a named function — a mutation removing the emptiness half
    survived the entire suite while it was inlined, because no test could
    construct the distinguishing input.
    """
    assert every_feed_unchanged({}) is False


def test_every_feed_unchanged_needs_all_of_them():
    """The other three arms, so the extraction cannot degrade into a
    `bool(unchanged)` that ignores the values."""
    assert every_feed_unchanged({"games": True}) is True
    assert every_feed_unchanged({"games": True, "pbp": True}) is True
    assert every_feed_unchanged({"games": True, "pbp": False}) is False
    assert every_feed_unchanged({"games": False}) is False


# --------------------------------------------------------------------------
# F6 — what RATE_PRECISION actually does
# --------------------------------------------------------------------------


async def test_a_published_rate_is_bounded_to_four_decimals(lake: SpyLake):
    """`RATE_PRECISION` is a **contract** property, not a stability one.

    The claim it used to carry — that rounding protects the digest from
    floating-point summation order — did not survive being checked: 300,000
    trials at PROE and clock magnitudes produced zero order-dependent sums,
    and `aggregate` folds in sorted-week order regardless. The comment in
    `rates.py` is corrected rather than propped up with a contrived fixture.

    What is true, and what this pins, is that consumers get four decimals.
    Widening `RATE_PRECISION` changes every published rate, which is
    observable here and was observable nowhere before.
    """
    from coaching_scheme.rates import RATE_PRECISION

    assert RATE_PRECISION == 4
    envelopes = await run_capture(lake=lake)
    rates = [
        (row["revision_id"], name, value)
        for row in envelopes[PROFILE].signals
        for name in (
            "neutral_pass_rate",
            "pass_rate_over_expected",
            "sec_per_play_neutral",
            "no_huddle_rate",
            "shotgun_rate",
            "play_action_rate",
            "pre_snap_motion_rate",
            "fourth_down_go_rate",
        )
        if (value := row[name]) is not None
    ]
    assert rates, "no populated rates — the fixture is broken, not the rounding"
    for revision_id, name, value in rates:
        assert value == round(value, 4), f"{revision_id}.{name} = {value!r}"

    personnel = [
        value
        for row in envelopes[PROFILE].signals
        if row["personnel_rates"]
        for value in row["personnel_rates"].values()
    ]
    assert personnel
    assert all(value == round(value, 4) for value in personnel)


def test_the_rounding_helper_actually_rounds():
    """A repeating decimal, so the mutant that widens the precision changes
    the answer rather than merely the type."""
    from coaching_scheme.rates import _rate

    assert _rate(1.0, 3.0) == 0.3333
    assert _rate(2.0, 7.0) == 0.2857

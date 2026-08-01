"""The unchanged-snapshot gate, conditional GET, and the durability gate.

A `seasonal` collector re-reads every 24 hours while this data changes once a
week. Three mechanisms keep that from filling an append-only lake with
identical objects, and each fails silently in its own way:

* **conditional GET** — bandwidth. A `304` is a *successful* capture.
* **the digest gate** — lake objects. Keyed by `(season, week, signal_type)`.
* **`PublishResult.landed`** — the durability gate. A digest recorded for a
  write that never landed permanently suppresses the retry.

Ported from `coaching-scheme`, where the same three were mutation-tested at
length. The one test that does not survive the port is the per-signal-type
independence case: there is only one signal type now, so the *claim* is
untestable behaviourally. The signal type stays in the digest key anyway — see
`capture.py` — and `test_the_digest_key_carries_the_signal_type` pins that
structurally so a future second type does not silently share one digest.
"""

import pytest
from collector_core.conditional import ETAGS, UpstreamUnchanged

from team_scheme.adapters import pbp as pbp_adapter
from team_scheme.capture import PROFILE, every_feed_unchanged

from .conftest import (
    LATER,
    SEASON,
    Feeds,
    SpyLake,
    proe_with_shift,
    run_capture,
)

# Every scope-sensitive test below runs at week 1 AND week 5. Both the digest
# key and the lake partition are keyed by `(season, week)`, and a literal week
# in either is a bug `officiating` actually shipped. Nothing about this
# collector is week-1 specific.
CAPTURE_WEEKS = (1, 5)

# One ETag per feed, so a second pass with the same content really 304s.
ALL_V1 = {"pbp": '"v1"', "ftn": '"v1"', "participation": '"v1"'}


@pytest.mark.parametrize("week", CAPTURE_WEEKS)
async def test_an_identical_second_pass_is_reported_unchanged(week, lake: SpyLake):
    """Byte-identical rows carry no information, so nothing is appended."""
    await run_capture(lake=lake, week=week)
    assert len(lake.writes) == 1

    with pytest.raises(UpstreamUnchanged):
        await run_capture(lake=lake, week=week, now=LATER)
    assert len(lake.writes) == 1


@pytest.mark.parametrize("week", CAPTURE_WEEKS)
async def test_a_changed_rate_publishes_again(week, lake: SpyLake):
    """The other side. Without it the gate could refuse every second pass
    unconditionally and the test above would still pass."""
    await run_capture(lake=lake, week=week)
    await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=20.0)),
        lake=lake,
        week=week,
        now=LATER,
    )
    assert len(lake.writes) == 2


async def test_the_digest_table_is_keyed_by_the_capture_week():
    """Two partitions, one process, identical content.

    **This is the sole killer of the symmetric hardcoding mutant.**
    Parameterising a test over the scoping key (as the two above do) catches
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

    assert len(lake.writes) == 2
    assert {envelope.scope["week"] for envelope in lake.writes} == {1, 5}


async def test_the_digest_table_is_keyed_by_the_season():
    """The other half of the scope key, for the same reason.

    Two seasons in one process with identical content. A digest keyed by week
    alone would report the second season unchanged and never write it — and on
    a `seasonal` cadence that is a whole season's object lost.
    """
    lake = SpyLake()
    await run_capture(lake=lake, season=SEASON, week=1)
    await run_capture(Feeds(season=2027), lake=lake, season=2027, week=1)
    assert len(lake.writes) == 2
    assert {envelope.scope["season"] for envelope in lake.writes} == {SEASON, 2027}


def test_the_digest_is_a_literal_sha256_and_never_a_salted_hash():
    """**A genuine landmine, pinned against a literal.**

    Python salts `hash()` on `str` per process, so a digest built from it
    differs between two pods and between one pod's restarts. Every pass then
    looks changed, the gate silently does nothing, and an append-only lake
    fills with byte-identical objects on a daily cadence — with no error
    anywhere.

    Nothing in a single-process suite can see that: `hash()` is perfectly
    stable within one run. So the value is pinned as a **literal** rather than
    recomputed — recomputing with `hashlib` would agree with any
    implementation on a single run, which is the same tautology the guard
    exists to avoid. Swapping in `hash()` returns a decimal string and dies
    here immediately.
    """
    from team_scheme.capture import _digest

    digest = _digest([{"team_id": "AAA", "season": 2026}])
    assert digest == (
        "20c7098e10e6712b8ccf3f13b1dff25aee9a2872bc5b2c5df8551a88d78f90b5"
    )
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


async def test_only_the_signal_types_that_were_published_are_asked_about():
    """**Iterate `published`, not `envelopes`.**

    `PublishResult.landed` *raises* for a signal type the call did not
    publish, and that raise fires after every write has already happened —
    nothing in the collector catches it, so `_run_capture`'s blanket handler
    drops the pass and `/signals` keeps serving the previous capture even
    though the lake write succeeded. An availability inversion caused by a
    bookkeeping loop.

    With one signal type the two loops are the same object, so the claim is
    unobservable through `capture_team_scheme` — mutating `published` to
    `envelopes` survived the whole suite. It is tested here directly against
    `_publish_changed` with two synthetic types, one of them already
    published, because that is the shape `coaching-staff` will create and the
    failure is silent until it does.
    """
    from collector_core.envelope import (
        ENVELOPE_VERSION,
        Coverage,
        Envelope,
        Upstream,
    )

    from team_scheme import capture as capture_module
    from team_scheme.capture import COLLECTOR_NAME

    def envelope(signal_type: str) -> Envelope:
        return Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector=COLLECTOR_NAME,
            signal_type=signal_type,
            captured_at=LATER,
            upstream=Upstream(adapter="x", fetched_at=LATER, source_ref=None),
            scope={"season": SEASON, "week": 3},
            coverage=Coverage(expected=1, present=1, missing=[]),
            errors=[],
            signals=[],
        )

    capture_module.reset_published_digests()
    capture_module._PUBLISHED_DIGESTS[(SEASON, 3, "already")] = "same"

    lake = SpyLake()
    result = await capture_module._publish_changed(
        {"already": envelope("already"), PROFILE: envelope(PROFILE)},
        {"already": "same", PROFILE: "new"},
        SEASON,
        3,
        lake,
    )

    # Only the changed one was written, and asking about the suppressed one
    # did not blow up the pass.
    assert [written.signal_type for written in lake.writes] == [PROFILE]
    assert set(result) == {"already", PROFILE}
    assert capture_module._PUBLISHED_DIGESTS[(SEASON, 3, PROFILE)] == "new"
    capture_module.reset_published_digests()


def test_the_digest_key_carries_the_signal_type():
    """Structural, because with one signal type the behaviour is unobservable.

    A per-pass key would have to be *rewritten* rather than extended when
    `coaching-staff` lands, and the failure of sharing one digest across two
    types is silent: a rate change re-appends a byte-identical staff envelope
    after every week's games, all 32 teams, every week.
    """
    from team_scheme import capture as capture_module

    capture_module.reset_published_digests()
    capture_module._PUBLISHED_DIGESTS[(2026, 1, PROFILE)] = "x"
    (key,) = capture_module._PUBLISHED_DIGESTS
    assert key == (2026, 1, PROFILE)
    assert len(key) == 3
    capture_module.reset_published_digests()


# --------------------------------------------------------------------------
# The durability gate
# --------------------------------------------------------------------------


async def test_a_failed_write_still_serves_the_capture():
    """Availability over durability: the capture succeeded and only its
    archival copy did not, so the envelopes come back anyway."""
    broken = SpyLake(fail_write=True)
    envelopes = await run_capture(lake=broken)
    assert set(envelopes) == {PROFILE}
    assert broken.writes == []


async def test_a_digest_is_not_recorded_for_a_write_that_never_landed():
    """**The permanent-suppression failure.**

    Pass 1 against a dead lake builds a correct envelope and writes nothing.
    Pass 2 against a healthy lake must write it. Record the digest without
    checking `landed` and pass 2 raises `UpstreamUnchanged` instead, and the
    object is never written until the upstream itself changes — on a seasonal
    cadence, a week at best and a season at worst.
    """
    broken = SpyLake(fail_write=True)
    await run_capture(lake=broken)

    healthy = SpyLake()
    await run_capture(lake=healthy, now=LATER)
    assert [envelope.signal_type for envelope in healthy.writes] == [PROFILE]


async def test_a_landed_write_does_record_its_digest(lake: SpyLake):
    """The other side. Without it, `landed` could return a constant `False`
    and the gate would be off entirely while the test above still passed."""
    await run_capture(lake=lake)
    with pytest.raises(UpstreamUnchanged):
        await run_capture(lake=lake, now=LATER)


# --------------------------------------------------------------------------
# Conditional GET across three feeds
# --------------------------------------------------------------------------


async def test_all_three_feeds_unchanged_is_a_successful_unchanged_pass(
    lake: SpyLake,
):
    """A 304 everywhere means the pass is unchanged — no envelope, no
    failure. Routing it into `fail_capture` would write `present: 0` over a
    healthy capture."""
    await run_capture(Feeds(etags=ALL_V1), lake=lake)
    assert len(lake.writes) == 1

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

    The three feeds are three separate nflverse releases built by three
    separate jobs, and the charting ones wait on a third party. So the common
    mixed case is play-by-play rebuilt after Sunday's games with charting
    still on last week's build — and re-raising the first `UpstreamUnchanged`
    (which `officiating` does) would discard a whole week of new rates to wait
    on a feed owning two fields of thirteen.

    Pass 1 caches every ETag. Pass 2 serves a *changed* play-by-play and 304s
    the other two, and must still publish rates **with the charted fields
    populated** — which is what the re-fetch is for.
    """
    await run_capture(Feeds(etags=ALL_V1), lake=lake)

    second = Feeds(
        proe=proe_with_shift("AAA", at_week=7, shift=20.0),
        etags={**ALL_V1, "pbp": '"v2"'},
    )
    envelopes = await run_capture(second, lake=lake, now=LATER)

    # Each 304'd feed was asked twice: once conditionally, then again with no
    # `If-None-Match` because another feed changed.
    assert second.requests.count(("ftn", '"v1"')) == 1
    assert second.requests.count(("ftn", None)) == 1
    assert second.requests.count(("participation", '"v1"')) == 1
    assert second.requests.count(("participation", None)) == 1

    rows = envelopes[PROFILE].signals
    assert rows
    # The re-read feeds really produced their fields, rather than the profile
    # silently degrading to nulls — the failure an absent re-fetch produces.
    assert all(row["play_action_rate"] is not None for row in rows)
    assert all(row["personnel_rates"] is not None for row in rows)
    assert envelopes[PROFILE].signals[0]["degraded_upstreams"] == []


async def test_a_changed_charting_feed_alone_also_publishes(lake: SpyLake):
    """The mirror, and the case that proves the re-fetch is symmetric.

    Play-by-play 304s and the charting feed changed. Without the
    unconditional re-read of pbp the pass has no play index at all, so every
    charted row would be unattributable and every rate would vanish.
    """
    await run_capture(Feeds(etags=ALL_V1), lake=lake)

    second = Feeds(etags={**ALL_V1, "ftn": '"v2"'})
    envelopes = await run_capture(second, lake=lake, now=LATER)
    assert second.requests.count(("pbp", None)) == 1
    rows = envelopes[PROFILE].signals
    assert len(rows) == 4
    assert all(row["neutral_pass_rate"] is not None for row in rows)


async def test_the_shared_etag_store_survives_an_unconditional_re_read(
    lake: SpyLake,
):
    """The re-fetch uses a throwaway store, so the shared one keeps its
    (still-correct) entry and the next pass 304s again normally. Committing
    the re-read's response into the shared store would be harmless here and
    wrong in general — it would overwrite a valid ETag with one obtained
    without `If-None-Match`.

    Watched on **play-by-play** while the *charting* feed is the one that
    changed, because it is the 304'd feed whose entry has to survive. Watching
    the changed feed would assert nothing: its ETag is supposed to move.
    """
    await run_capture(Feeds(etags=ALL_V1), lake=lake)
    before = ETAGS.get(pbp_adapter.source_ref(SEASON))
    assert before == '"v1"'

    await run_capture(
        Feeds(etags={**ALL_V1, "ftn": '"v2"'}),
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

    The caller cannot currently produce an empty map: a play-by-play failure
    ends the pass before the check is reached. That is exactly why the
    predicate is a named function — a mutation removing the emptiness half
    survived the entire `coaching-scheme` suite while it was inlined, because
    no test could construct the distinguishing input.
    """
    assert every_feed_unchanged({}) is False


def test_every_feed_unchanged_needs_all_of_them():
    """The other three arms, so the extraction cannot degrade into a
    `bool(unchanged)` that ignores the values."""
    assert every_feed_unchanged({"pbp": True}) is True
    assert every_feed_unchanged({"pbp": True, "ftn": True}) is True
    assert every_feed_unchanged({"pbp": True, "ftn": False}) is False
    assert every_feed_unchanged({"pbp": False}) is False


# --------------------------------------------------------------------------
# What RATE_PRECISION actually does
# --------------------------------------------------------------------------


async def test_a_published_rate_is_bounded_to_four_decimals(lake: SpyLake):
    """`RATE_PRECISION` is a **contract** property, not a stability one.

    The claim it used to carry in `coaching-scheme` — that rounding protects
    the digest from floating-point summation order — did not survive being
    checked: 300,000 trials at PROE and clock magnitudes produced zero
    order-dependent sums, and `aggregate` folds in sorted-week order
    regardless. The correction is carried across rather than the claim.

    What is true, and what this pins, is that consumers get four decimals.
    Widening `RATE_PRECISION` changes every published rate.
    """
    from team_scheme.rates import RATE_PRECISION

    assert RATE_PRECISION == 4
    envelopes = await run_capture(lake=lake)
    rates = [
        (row["team_id"], name, value)
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
    for team_id, name, value in rates:
        assert value == round(value, 4), f"{team_id}.{name} = {value!r}"

    personnel = [
        value
        for row in envelopes[PROFILE].signals
        if row["personnel_rates"]
        for value in row["personnel_rates"].values()
    ]
    assert personnel
    assert all(value == round(value, 4) for value in personnel)


def test_the_rounding_helper_actually_rounds():
    """A repeating decimal, so a mutant that widens OR narrows the precision
    changes the answer rather than merely the type."""
    from team_scheme.rates import _rate

    assert _rate(1.0, 3.0) == 0.3333
    assert _rate(2.0, 7.0) == 0.2857


def test_an_empty_denominator_is_null_rather_than_zero():
    """`None` means 'no evidence'; 0.0 means 'measured, and it was zero'. A
    team with no neutral snaps has no neutral pass rate, and reporting 0.0
    would say it ran on every one of them — and would count it present."""
    from team_scheme.rates import _rate

    assert _rate(0.0, 0.0) is None
    assert _rate(5.0, 0.0) is None
    assert _rate(0.0, 4.0) == 0.0

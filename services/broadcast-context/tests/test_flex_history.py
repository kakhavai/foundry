"""The point-in-time guard: flex derivation, and the two-snapshot evidence rule.

This is the collector's product and the spec's named failure mode, so the
fixtures are built one per arm: flexed-in versus flexed-out, one snapshot
versus two, a window change versus a kickoff change, a change to this game
versus a change to its slot-mate. A guard whose two arms share a fixture
cannot tell its sides apart.
"""

from datetime import UTC, datetime, timedelta

import pytest

from broadcast_context.history import (
    FLEX_IN,
    FLEX_ORIGINAL,
    FLEX_OUT,
    FLEX_TIME_CHANGED,
    MAX_HISTORY_SNAPSHOTS,
    History,
    ObservedState,
    classify_transition,
    derive_flex,
    flex_is_evidenced,
    read_history,
)

from .conftest import SEASON, SpyLake, feed_document, run_capture, snapshot, week_rows

T1 = "2026-09-01T12:00:00Z"
T2 = "2026-10-01T12:00:00Z"
NOW_ISO = "2026-11-01T12:00:00Z"

SUN_LATE_KICK = "2026-11-15T21:25:00Z"
SNF_KICK = "2026-11-16T01:20:00Z"


def state(window_id, kickoff, captured_at, games_in_window=1):
    return ObservedState(window_id, kickoff, games_in_window, captured_at)


def flex(prior, *, window_id, kickoff_at, games_in_window=1, now=NOW_ISO):
    return derive_flex(
        prior,
        window_id=window_id,
        kickoff_at=kickoff_at,
        games_in_window=games_in_window,
        now=now,
    )


# --------------------------------------------------------------------------
# classify_transition
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("sun_early", "snf", FLEX_IN),
        ("sun_late", "mnf", FLEX_IN),
        ("sun_early", "tnf", FLEX_IN),
        ("snf", "sun_late", FLEX_OUT),
        ("mnf", "sun_early", FLEX_OUT),
        # Neither side is a primetime package: a move between two afternoon
        # windows is a time change, not a flex in or out.
        ("sun_early", "sun_late", FLEX_TIME_CHANGED),
        # Both sides are primetime packages — snf to mnf is a move, but it is
        # not "into" or "out of" primetime.
        ("snf", "mnf", FLEX_TIME_CHANGED),
        # Same window: only the kickoff instant moved.
        ("sun_early", "sun_early", FLEX_TIME_CHANGED),
        # A previously unassigned window becoming primetime is still a flex in.
        (None, "snf", FLEX_IN),
    ],
)
def test_transition_classification(previous, current, expected):
    assert classify_transition(previous, current) == expected


# --------------------------------------------------------------------------
# derive_flex
# --------------------------------------------------------------------------


def test_a_first_capture_claims_no_history():
    """The spec's own consequence: on a first capture every game is
    `original`, and that is correct. Inventing a flex history out of one fetch
    is the failure this collector exists to avoid."""
    verdict = flex((), window_id="sun_early", kickoff_at=SUN_LATE_KICK)
    assert verdict.flex_status == FLEX_ORIGINAL
    assert verdict.previous_window_id is None
    assert verdict.first_observed_at == NOW_ISO
    assert verdict.observed_window_count == 1


def test_an_unchanged_state_keeps_the_instant_it_was_first_seen():
    """`first_observed_at` must NOT be recomputed as `now` on a pass that
    changed nothing.

    Two failures ride on this one line. It would restate an old fact as
    brand-new to any `as_of` query — retroactive certainty arriving by the
    back door — and it would make every daily pass produce a different digest,
    so the append-only lake would fill with snapshots differing in that field
    alone.
    """
    prior = [state("sun_early", SUN_LATE_KICK, T1)]
    verdict = flex(prior, window_id="sun_early", kickoff_at=SUN_LATE_KICK)
    assert verdict.first_observed_at == T1
    assert verdict.flex_status == FLEX_ORIGINAL
    assert verdict.observed_window_count == 1


def test_a_flex_into_primetime_this_pass():
    prior = [state("sun_early", SUN_LATE_KICK, T1)]
    verdict = flex(prior, window_id="snf", kickoff_at=SNF_KICK)
    assert verdict.flex_status == FLEX_IN
    assert verdict.previous_window_id == "sun_early"
    assert verdict.first_observed_at == NOW_ISO
    assert verdict.observed_window_count == 2


def test_a_flex_out_of_primetime_this_pass():
    """The other arm, with its own fixture. A set that only ever moves games
    INTO primetime passes with `flexed_out` deleted entirely."""
    prior = [state("snf", SNF_KICK, T1)]
    verdict = flex(prior, window_id="sun_late", kickoff_at=SUN_LATE_KICK)
    assert verdict.flex_status == FLEX_OUT
    assert verdict.previous_window_id == "snf"
    assert verdict.observed_window_count == 2


def test_a_kickoff_move_inside_one_window_is_a_time_change():
    prior = [state("sun_early", "2026-11-15T18:00:00Z", T1)]
    verdict = flex(prior, window_id="sun_early", kickoff_at="2026-11-15T20:00:00Z")
    assert verdict.flex_status == FLEX_TIME_CHANGED
    # The spec's field says "the window this game held before the most recent
    # change". For a time change the window did not move, so it is equal to
    # the current one rather than null — null would read as `original`.
    assert verdict.previous_window_id == "sun_early"
    assert verdict.observed_window_count == 2


def test_a_change_observed_on_an_earlier_pass_still_reports_itself():
    """Two snapshots already in the lake, and this pass changed nothing.

    The status and `previous_window_id` come from the last transition in the
    chain, not from this pass — and `first_observed_at` stays pinned to the
    snapshot that first showed the current state.
    """
    prior = [
        state("sun_early", SUN_LATE_KICK, T1),
        state("snf", SNF_KICK, T2),
    ]
    verdict = flex(prior, window_id="snf", kickoff_at=SNF_KICK)
    assert verdict.flex_status == FLEX_IN
    assert verdict.previous_window_id == "sun_early"
    assert verdict.first_observed_at == T2
    assert verdict.observed_window_count == 2


def test_a_second_change_reports_the_most_recent_one():
    prior = [
        state("sun_early", SUN_LATE_KICK, T1),
        state("snf", SNF_KICK, T2),
    ]
    verdict = flex(prior, window_id="sun_late", kickoff_at=SUN_LATE_KICK)
    assert verdict.flex_status == FLEX_OUT
    assert verdict.previous_window_id == "snf"
    assert verdict.first_observed_at == NOW_ISO
    assert verdict.observed_window_count == 3


# --------------------------------------------------------------------------
# The slate-fact leak: `games_in_window` is part of the point-in-time state
# --------------------------------------------------------------------------


def test_a_slot_population_change_advances_first_observed_at():
    """**The leak, closed.** This game did not move; its slot-mate did.

    `games_in_window`, `is_standalone` and `distribution` are recomputed from
    today's slate, so leaving the count out of the observed state let a game
    whose own window never moved keep its early `first_observed_at`, pass the
    `as_of` filter, and arrive carrying a post-cutoff slot count. With the
    count in the state the instant advances, so an earlier `as_of` WITHHOLDS
    the row instead of admitting it wrong.
    """
    prior = [state("sun_late", SUN_LATE_KICK, T1, games_in_window=2)]
    verdict = flex(
        prior, window_id="sun_late", kickoff_at=SUN_LATE_KICK, games_in_window=1
    )
    assert verdict.first_observed_at == NOW_ISO
    # The state chain grew, but the game has still only ever been in ONE
    # window — which is what `observed_window_count` counts, and why it counts
    # transitions rather than `len(evidence)`.
    assert len(verdict.evidence) == 2
    assert verdict.observed_window_count == 1


def test_a_slot_population_change_is_not_reported_as_a_flex():
    """The other half of the same fix, and it needs its own assertion.

    `classify_transition` returns `time_changed` whenever the two windows are
    equal, so reading the raw chain would report a fabricated `time_changed`
    for a game whose kickoff never moved — and that claim would then need
    evidence it does not have.
    """
    prior = [state("sun_late", SUN_LATE_KICK, T1, games_in_window=2)]
    verdict = flex(
        prior, window_id="sun_late", kickoff_at=SUN_LATE_KICK, games_in_window=1
    )
    assert verdict.flex_status == FLEX_ORIGINAL
    assert verdict.previous_window_id is None


@pytest.mark.parametrize(
    ("prior", "window_id", "kickoff", "count"),
    [
        # Never moved, one state.
        ([], "sun_late", SUN_LATE_KICK, 1),
        # Never moved, two states — a slot-mate did. Counting states here
        # would report 2 for a game that has held one window throughout, and
        # the field name would contradict the field.
        (
            [state("sun_late", SUN_LATE_KICK, T1, games_in_window=2)],
            "sun_late",
            SUN_LATE_KICK,
            1,
        ),
        # Moved once.
        ([state("sun_early", SUN_LATE_KICK, T1)], "snf", SNF_KICK, 1),
        # Moved twice.
        (
            [state("sun_early", SUN_LATE_KICK, T1), state("snf", SNF_KICK, T2)],
            "sun_late",
            SUN_LATE_KICK,
            1,
        ),
    ],
)
def test_observed_window_count_is_one_exactly_when_the_status_is_original(
    prior, window_id, kickoff, count
):
    """The biconditional the rename bought, checkable on a single row.

    `observed_window_count == 1` **if and only if** `flex_status ==
    "original"`. Both directions are asserted here, over a case list that
    includes the slot-mate case in which the old state-count definition
    reported 2 for a game that never moved.
    """
    verdict = flex(
        prior,
        window_id=window_id,
        kickoff_at=kickoff,
        games_in_window=count,
    )
    assert (verdict.observed_window_count == 1) == (
        verdict.flex_status == FLEX_ORIGINAL
    ), verdict


def test_a_real_flex_still_reports_itself_across_a_slot_change():
    """The dimensions must stay separable in both directions: a chain whose
    middle entry is a slot-count-only change must still report the window move
    that came before it."""
    prior = [
        state("sun_early", SUN_LATE_KICK, T1, games_in_window=8),
        state("snf", SNF_KICK, T2, games_in_window=1),
    ]
    verdict = flex(prior, window_id="snf", kickoff_at=SNF_KICK, games_in_window=2)
    assert verdict.flex_status == FLEX_IN
    assert verdict.previous_window_id == "sun_early"
    assert verdict.first_observed_at == NOW_ISO


# --------------------------------------------------------------------------
# flex_is_evidenced — the spec's consistency check
# --------------------------------------------------------------------------


def test_original_needs_no_evidence():
    assert flex_is_evidenced(FLEX_ORIGINAL, ()) is True
    assert flex_is_evidenced(FLEX_ORIGINAL, [state("snf", SNF_KICK, T1)]) is True


@pytest.mark.parametrize("status", [FLEX_IN, FLEX_OUT, FLEX_TIME_CHANGED])
def test_a_change_claimed_from_one_snapshot_is_refused(status):
    """The spec's rule, exactly: one snapshot plus a non-original status means
    the earlier state was never captured, so the record is claiming a history
    it cannot evidence."""
    assert flex_is_evidenced(status, [state("snf", SNF_KICK, T1)]) is False


def test_a_flex_needs_two_distinct_windows_not_merely_two_snapshots():
    """Two snapshots carrying the SAME window do not evidence a flex."""
    same_window = [
        state("snf", SNF_KICK, T1),
        state("snf", "2026-11-16T01:15:00Z", T2),
    ]
    assert flex_is_evidenced(FLEX_IN, same_window) is False
    assert flex_is_evidenced(FLEX_OUT, same_window) is False


def test_a_time_change_is_evidenced_by_two_kickoffs_in_one_window():
    """The spec's own check, generalised to the status its enum admits.

    Read literally — "two distinct snapshots with differing `window_id`" — the
    rule rejects every legitimate `time_changed`, which by construction has
    one window and two kickoffs. Disclosed in the README.
    """
    same_window = [
        state("sun_early", "2026-11-15T18:00:00Z", T1),
        state("sun_early", "2026-11-15T20:00:00Z", T2),
    ]
    assert flex_is_evidenced(FLEX_TIME_CHANGED, same_window) is True
    # And an identical pair still is not evidence of anything.
    identical = [
        state("sun_early", "2026-11-15T18:00:00Z", T1),
        state("sun_early", "2026-11-15T18:00:00Z", T2),
    ]
    assert flex_is_evidenced(FLEX_TIME_CHANGED, identical) is False


def test_a_slot_count_difference_is_not_evidence_of_a_time_change():
    """The evidence check reads `window_state`, not `broadcast_state`.

    Two states differing only in `games_in_window` are evidence that a
    DIFFERENT game moved. Accepting them would let the slot-count fix hand a
    fabricated `time_changed` the evidence it needs to be published.
    """
    slot_change_only = [
        state("sun_late", SUN_LATE_KICK, T1, games_in_window=2),
        state("sun_late", SUN_LATE_KICK, T2, games_in_window=1),
    ]
    assert flex_is_evidenced(FLEX_TIME_CHANGED, slot_change_only) is False


def test_a_flex_with_two_distinct_windows_is_evidenced():
    evidence = [
        state("sun_early", SUN_LATE_KICK, T1),
        state("snf", SNF_KICK, T2),
    ]
    assert flex_is_evidenced(FLEX_IN, evidence) is True
    assert flex_is_evidenced(FLEX_OUT, evidence) is True


def test_every_derived_verdict_is_self_evidencing():
    """A property, over every transition a real pass can produce.

    Paired with a length assertion: `all([])` is `True`, and an empty case
    list would make this test pass while checking nothing.
    """
    cases = [
        ((), "sun_early", SUN_LATE_KICK, 1),
        ([state("sun_early", SUN_LATE_KICK, T1)], "snf", SNF_KICK, 1),
        ([state("snf", SNF_KICK, T1)], "sun_late", SUN_LATE_KICK, 1),
        (
            [state("sun_early", "2026-11-15T18:00:00Z", T1)],
            "sun_early",
            "2026-11-15T20:00:00Z",
            1,
        ),
        ([state("sun_early", SUN_LATE_KICK, T1)], "sun_early", SUN_LATE_KICK, 1),
        # The slot-count-only change, which must derive as `original` and so
        # need no evidence at all.
        (
            [state("sun_late", SUN_LATE_KICK, T1, games_in_window=2)],
            "sun_late",
            SUN_LATE_KICK,
            1,
        ),
    ]
    assert len(cases) == 6
    for prior, window_id, kickoff, count in cases:
        verdict = flex(
            prior, window_id=window_id, kickoff_at=kickoff, games_in_window=count
        )
        assert flex_is_evidenced(verdict.flex_status, verdict.evidence), verdict


# --------------------------------------------------------------------------
# read_history
# --------------------------------------------------------------------------


async def _read(lake, *, week=1, limit=MAX_HISTORY_SNAPSHOTS):
    return await read_history(
        lake,
        collector="broadcast-context",
        signal_type="game_broadcast_window",
        season=SEASON,
        week=week,
        limit=limit,
    )


def _row(game_id, window_id, kickoff, *, games_in_window=1, week=1):
    return {
        "game_id": game_id,
        "window_id": window_id,
        "kickoff_at": kickoff,
        "games_in_window": games_in_window,
        "week": week,
    }


async def test_an_empty_partition_yields_no_history():
    history = await _read(SpyLake())
    assert history.chains == {}
    assert history.truncated == 0
    assert history.week_high_water == {}


async def test_snapshots_fold_oldest_first_and_dedupe_repeats():
    """The chain records DISTINCT states, so its length is the number of
    things that happened to the game rather than the number of times we
    looked — which is what `observed_window_count` and the evidence check
    both read."""
    lake = SpyLake()
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    snapshot(lake, [_row("g1", "sun_early", SUN_LATE_KICK)], captured_at=base)
    snapshot(
        lake,
        [_row("g1", "sun_early", SUN_LATE_KICK)],
        captured_at=base + timedelta(days=1),
    )
    snapshot(
        lake,
        [_row("g1", "snf", SNF_KICK)],
        captured_at=base + timedelta(days=2),
    )

    history = await _read(lake)
    assert history.truncated == 0
    chain = history.chains["g1"]
    assert [s.window_id for s in chain] == ["sun_early", "snf"]
    # The FIRST appearance, not the latest snapshot still showing it.
    assert chain[0].captured_at == "2026-09-01T12:00:00Z"
    assert chain[1].captured_at == "2026-09-03T12:00:00Z"


async def test_a_slot_count_change_alone_appends_to_the_chain():
    """The dedupe key is `broadcast_state`, so a snapshot differing only in
    `games_in_window` is a distinct observed state — that is what makes the
    instant advance and the row get withheld."""
    lake = SpyLake()
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    snapshot(
        lake,
        [_row("g1", "sun_late", SUN_LATE_KICK, games_in_window=2)],
        captured_at=base,
    )
    snapshot(
        lake,
        [_row("g1", "sun_late", SUN_LATE_KICK, games_in_window=1)],
        captured_at=base + timedelta(days=1),
    )

    chain = (await _read(lake)).chains["g1"]
    assert [s.games_in_window for s in chain] == [2, 1]
    assert chain[1].captured_at == "2026-09-02T12:00:00Z"


async def test_the_week_baseline_is_a_high_water_mark_not_the_newest_count():
    """**The R2 fix at its source.** Against the newest count, a truncation
    that persists is flagged once and then goes quiet forever. The baseline
    must describe the most this week has ever been seen to hold."""
    lake = SpyLake()
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    for index, count in enumerate((3, 5, 4)):
        snapshot(
            lake,
            [_row(f"g{i}", "sun_early", SUN_LATE_KICK, week=1) for i in range(count)],
            captured_at=base + timedelta(days=index),
        )
    assert (await _read(lake)).week_high_water == {1: 5}


async def test_the_high_water_mark_is_per_week_and_never_summed():
    """Max-merged per snapshot, never accumulated across them — a sum would
    grow without bound and flag every week forever."""
    lake = SpyLake()
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    for index in range(3):
        snapshot(
            lake,
            [
                *[_row(f"a{i}", "sun_early", SUN_LATE_KICK, week=1) for i in range(4)],
                *[_row(f"b{i}", "sun_early", SUN_LATE_KICK, week=2) for i in range(6)],
            ],
            captured_at=base + timedelta(days=index),
        )
    assert (await _read(lake)).week_high_water == {1: 4, 2: 6}


async def test_an_empty_failure_envelope_cannot_lower_the_baseline():
    """`fail_capture` writes a `present: 0` envelope with no rows. Under a
    max-merge that is structurally unable to lower anything, which is why the
    explicit `if rows:` guard this used to need is gone."""
    lake = SpyLake()
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    snapshot(
        lake,
        [_row(f"g{i}", "sun_early", SUN_LATE_KICK, week=1) for i in range(5)],
        captured_at=base,
    )
    snapshot(lake, [], captured_at=base + timedelta(days=1))

    assert (await _read(lake)).week_high_water == {1: 5}


async def test_history_is_read_from_the_partition_the_capture_writes():
    """A snapshot in another week's partition is not this week's history."""
    lake = SpyLake()
    snapshot(
        lake,
        [_row("g1", "snf", SNF_KICK)],
        captured_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        week=5,
    )
    assert (await _read(lake, week=1)).chains == {}
    assert list((await _read(lake, week=5)).chains) == ["g1"]


async def test_reading_past_the_cap_keeps_the_newest_and_says_how_many_it_dropped():
    """Truncation keeps the RECENT end, which is the only safe direction:
    `previous_window_id` and the evidence check both read it, and dropping the
    oldest can only move `first_observed_at` later than the truth — which
    under-claims knowledge rather than over-claiming it."""
    lake = SpyLake()
    base = datetime(2026, 9, 1, 12, tzinfo=UTC)
    for index in range(5):
        snapshot(
            lake,
            [_row("g1", f"w{index}", SNF_KICK)],
            captured_at=base + timedelta(days=index),
        )

    history = await _read(lake, limit=2)
    assert history.truncated == 3
    assert [s.window_id for s in history.chains["g1"]] == ["w3", "w4"]


async def test_a_row_without_a_game_id_is_skipped_not_keyed_on_none():
    lake = SpyLake()
    snapshot(
        lake,
        [{"window_id": "snf", "kickoff_at": SNF_KICK}, _row("g1", "snf", SNF_KICK)],
        captured_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
    )
    assert list((await _read(lake)).chains) == ["g1"]


async def test_a_snapshot_with_no_capture_instant_is_skipped():
    """An object with no `captured_at` cannot be placed in time, and folding
    it in would attach `first_observed_at: None` to whatever it carried — a
    row that then fails the `as_of` filter closed for the rest of the season."""
    lake = SpyLake()
    snapshot(
        lake,
        [_row("g1", "snf", SNF_KICK)],
        captured_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
    )
    key = next(iter(lake.objects))
    lake.objects[key] = {**lake.objects[key], "captured_at": None}

    assert (await _read(lake)).chains == {}


async def test_a_lake_failure_propagates_rather_than_degrading_to_no_history():
    """Degrading would publish `original` for games previously recorded as
    flexed, into an append-only lake where that claim becomes the evidence the
    next pass reads back. Losing a pass is recoverable; that is not."""

    class BrokenLake(SpyLake):
        def list_keys(self, *args, **kwargs):
            raise RuntimeError("object store unreachable")

    with pytest.raises(RuntimeError):
        await _read(BrokenLake())


# --------------------------------------------------------------------------
# The guard, through the capture pass
# --------------------------------------------------------------------------


async def test_a_row_claiming_an_unevidenced_flex_is_refused(monkeypatch):
    """The spec's consistency check, enforced at WRITE time.

    A derived status cannot normally fail it, so this stubs the derivation to
    produce exactly the record the spec describes — a non-`original` status
    with one observed state — and asserts the pass refuses to emit it. Without
    the check that row reaches an append-only lake, where it becomes the
    evidence the next pass reads back.
    """
    from broadcast_context import capture as capture_module

    def unevidenced(prior, *, window_id, kickoff_at, games_in_window, now):
        return capture_module.FlexVerdict(
            FLEX_IN,
            "sun_early",
            now,
            (ObservedState(window_id, kickoff_at, games_in_window, now),),
        )

    monkeypatch.setattr(capture_module, "derive_flex", unevidenced)
    envelopes = await run_capture(feed_document(week_rows(1)), lake=SpyLake())
    envelope = envelopes[capture_module.SIGNAL]

    assert envelope.signals == []
    assert envelope.coverage.present == 0
    assert "flex_history_unevidenced" in {error["reason"] for error in envelope.errors}


async def test_a_truncated_history_is_stated_in_the_envelope(monkeypatch):
    """The cap is a bound on a pathological case, but a pass that hit it has
    read an incomplete chain and must say so — silently reporting
    `first_observed_at` from the oldest snapshot it happened to keep is the
    kind of quiet degradation an operator cannot see."""
    from broadcast_context import capture as capture_module

    async def truncated(*args, **kwargs):
        return History(chains={}, truncated=3, week_high_water={})

    monkeypatch.setattr(capture_module, "read_history", truncated)
    envelopes = await run_capture(feed_document(week_rows(1)), lake=SpyLake())

    entries = [
        error
        for error in envelopes[capture_module.SIGNAL].errors
        if error["reason"] == "history_truncated"
    ]
    assert len(entries) == 1
    assert "3 snapshot(s)" in entries[0]["detail"]

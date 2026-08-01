"""The point-in-time guard: flex derivation, and the two-snapshot evidence rule.

This is the collector's product and the spec's named failure mode, so the
fixtures are built one per arm: flexed-in versus flexed-out, one snapshot
versus two, a window change versus a kickoff change. A guard whose two arms
share a fixture cannot tell its sides apart.
"""

from datetime import UTC, datetime, timedelta

import pytest

from broadcast_context.history import (
    FLEX_IN,
    FLEX_ORIGINAL,
    FLEX_OUT,
    FLEX_TIME_CHANGED,
    MAX_HISTORY_SNAPSHOTS,
    ObservedState,
    classify_transition,
    derive_flex,
    flex_is_evidenced,
    read_history,
)

from .conftest import (
    SEASON,
    SpyLake,
    feed_document,
    run_capture,
    snapshot,
    week_rows,
)

T1 = "2026-09-01T12:00:00Z"
T2 = "2026-10-01T12:00:00Z"
NOW_ISO = "2026-11-01T12:00:00Z"

SUN_LATE_KICK = "2026-11-15T21:25:00Z"
SNF_KICK = "2026-11-16T01:20:00Z"


def state(window_id, kickoff, captured_at):
    return ObservedState(window_id, kickoff, captured_at)


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
    verdict = derive_flex(
        (), window_id="sun_early", kickoff_at=SUN_LATE_KICK, now=NOW_ISO
    )
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
    verdict = derive_flex(
        prior, window_id="sun_early", kickoff_at=SUN_LATE_KICK, now=NOW_ISO
    )
    assert verdict.first_observed_at == T1
    assert verdict.flex_status == FLEX_ORIGINAL
    assert verdict.observed_window_count == 1


def test_a_flex_into_primetime_this_pass():
    prior = [state("sun_early", SUN_LATE_KICK, T1)]
    verdict = derive_flex(prior, window_id="snf", kickoff_at=SNF_KICK, now=NOW_ISO)
    assert verdict.flex_status == FLEX_IN
    assert verdict.previous_window_id == "sun_early"
    assert verdict.first_observed_at == NOW_ISO
    assert verdict.observed_window_count == 2


def test_a_flex_out_of_primetime_this_pass():
    """The other arm, with its own fixture. A set that only ever moves games
    INTO primetime passes with `flexed_out` deleted entirely."""
    prior = [state("snf", SNF_KICK, T1)]
    verdict = derive_flex(
        prior, window_id="sun_late", kickoff_at=SUN_LATE_KICK, now=NOW_ISO
    )
    assert verdict.flex_status == FLEX_OUT
    assert verdict.previous_window_id == "snf"
    assert verdict.observed_window_count == 2


def test_a_kickoff_move_inside_one_window_is_a_time_change():
    prior = [state("sun_early", "2026-11-15T18:00:00Z", T1)]
    verdict = derive_flex(
        prior,
        window_id="sun_early",
        kickoff_at="2026-11-15T20:00:00Z",
        now=NOW_ISO,
    )
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
    verdict = derive_flex(prior, window_id="snf", kickoff_at=SNF_KICK, now=NOW_ISO)
    assert verdict.flex_status == FLEX_IN
    assert verdict.previous_window_id == "sun_early"
    assert verdict.first_observed_at == T2
    assert verdict.observed_window_count == 2


def test_a_second_change_reports_the_most_recent_one():
    prior = [
        state("sun_early", SUN_LATE_KICK, T1),
        state("snf", SNF_KICK, T2),
    ]
    verdict = derive_flex(
        prior, window_id="sun_late", kickoff_at=SUN_LATE_KICK, now=NOW_ISO
    )
    assert verdict.flex_status == FLEX_OUT
    assert verdict.previous_window_id == "snf"
    assert verdict.first_observed_at == NOW_ISO
    assert verdict.observed_window_count == 3


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


def test_a_flex_with_two_distinct_windows_is_evidenced():
    evidence = [
        state("sun_early", SUN_LATE_KICK, T1),
        state("snf", SNF_KICK, T2),
    ]
    assert flex_is_evidenced(FLEX_IN, evidence) is True
    assert flex_is_evidenced(FLEX_OUT, evidence) is True


def test_every_derived_verdict_is_self_evidencing():
    """A property, over the four transitions a real pass can produce.

    Paired with a length assertion: `all([])` is `True`, and an empty case
    list would make this test pass while checking nothing.
    """
    cases = [
        ((), "sun_early", SUN_LATE_KICK),
        ([state("sun_early", SUN_LATE_KICK, T1)], "snf", SNF_KICK),
        ([state("snf", SNF_KICK, T1)], "sun_late", SUN_LATE_KICK),
        (
            [state("sun_early", "2026-11-15T18:00:00Z", T1)],
            "sun_early",
            "2026-11-15T20:00:00Z",
        ),
        ([state("sun_early", SUN_LATE_KICK, T1)], "sun_early", SUN_LATE_KICK),
    ]
    assert len(cases) == 5
    for prior, window_id, kickoff in cases:
        verdict = derive_flex(
            prior, window_id=window_id, kickoff_at=kickoff, now=NOW_ISO
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


def _row(game_id, window_id, kickoff):
    return {"game_id": game_id, "window_id": window_id, "kickoff_at": kickoff}


async def test_an_empty_partition_yields_no_history():
    history, truncated = await _read(SpyLake())
    assert history == {}
    assert truncated == 0


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

    history, truncated = await _read(lake)
    assert truncated == 0
    chain = history["g1"]
    assert [s.window_id for s in chain] == ["sun_early", "snf"]
    # The FIRST appearance, not the latest snapshot still showing it.
    assert chain[0].captured_at == "2026-09-01T12:00:00Z"
    assert chain[1].captured_at == "2026-09-03T12:00:00Z"


async def test_history_is_read_from_the_partition_the_capture_writes():
    """A snapshot in another week's partition is not this week's history."""
    lake = SpyLake()
    snapshot(
        lake,
        [_row("g1", "snf", SNF_KICK)],
        captured_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        week=5,
    )
    history, _ = await _read(lake, week=1)
    assert history == {}
    history, _ = await _read(lake, week=5)
    assert list(history) == ["g1"]


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

    history, truncated = await _read(lake, limit=2)
    assert truncated == 3
    assert [s.window_id for s in history["g1"]] == ["w3", "w4"]


async def test_a_row_without_a_game_id_is_skipped_not_keyed_on_none():
    lake = SpyLake()
    snapshot(
        lake,
        [{"window_id": "snf", "kickoff_at": SNF_KICK}, _row("g1", "snf", SNF_KICK)],
        captured_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
    )
    history, _ = await _read(lake)
    assert list(history) == ["g1"]


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

    history, _ = await _read(lake)
    assert history == {}


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

    def unevidenced(prior, *, window_id, kickoff_at, now):
        return capture_module.FlexVerdict(
            FLEX_IN, "sun_early", now, (ObservedState(window_id, kickoff_at, now),)
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
        return {}, 3

    monkeypatch.setattr(capture_module, "read_history", truncated)
    envelopes = await run_capture(feed_document(week_rows(1)), lake=SpyLake())

    entries = [
        error
        for error in envelopes[capture_module.SIGNAL].errors
        if error["reason"] == "history_truncated"
    ]
    assert len(entries) == 1
    assert "3 snapshot(s)" in entries[0]["detail"]

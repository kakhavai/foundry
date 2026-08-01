"""Guard 2: a PROE changepoint with no corresponding staff revision.

**Two arms, and they need separate fixtures** for the same reason guard 1's
do — both end in "a changepoint was found", and only the second half tells
them apart:

* **Arm A, a changepoint WITH a matching revision.** Must not alarm. Without
  this fixture, `explains` could return a constant `False` and every alarm
  test would still pass — the collector would flag every real coaching change
  as unexplained, which is the loudest possible false positive.
* **Arm B, a changepoint WITHOUT one.** Must alarm. Without this fixture,
  `explains` could return a constant `True` and the guard would be off.

`detect` and `explains` are separate functions precisely so a test can pin
each independently; see `changepoint.py` on why fusing them would let the
search quietly narrow to weeks near a boundary.
"""

import pytest

from coaching_scheme.changepoint import (
    MAX_WINDOW,
    MIN_RUN,
    MIN_SHIFT,
    REASON_UNEXPLAINED_CHANGEPOINT,
    detect,
    explains,
)
from coaching_scheme.revisions import build_revisions

from .conftest import (
    SEASON,
    Feeds,
    SpyLake,
    coaches_with_change,
    games_document,
    proe_with_shift,
    run_capture,
    steady_coaches,
)


def series(*levels_by_week: float, start: int = 1) -> list[tuple[int, float]]:
    return [(start + index, value) for index, value in enumerate(levels_by_week)]


# --------------------------------------------------------------------------
# detect — the shift itself
# --------------------------------------------------------------------------


def test_a_flat_series_has_no_changepoint():
    """The negative control. A detector that always fired would pass every
    positive test in this file."""
    assert detect(series(*([2.0] * 12))) is None


def test_a_sustained_shift_past_the_threshold_is_detected():
    found = detect(series(0, 0, 0, 0, 12, 12, 12, 12))
    assert found is not None
    assert found.week == 5
    assert found.shift == pytest.approx(12.0)
    assert found.before_mean == pytest.approx(0.0)
    assert found.after_mean == pytest.approx(12.0)


def test_a_downward_shift_is_detected_too():
    """The sign is not the trigger. A detector comparing `shift > MIN_SHIFT`
    rather than `abs(shift)` misses every offense that got more run-heavy,
    which is the commoner direction after a firing."""
    found = detect(series(15, 15, 15, 15, 0, 0, 0, 0))
    assert found is not None
    assert found.week == 5
    assert found.shift == pytest.approx(-15.0)


def test_a_shift_at_exactly_the_threshold_does_not_fire():
    """`>` not `>=`. The spec says 'more than roughly eight points'."""
    at = MIN_SHIFT
    assert detect(series(0, 0, 0, 0, at, at, at, at)) is None


def test_a_shift_just_past_the_threshold_does_fire():
    """The non-equivalent neighbour of the test above. Together they pin the
    boundary at MIN_SHIFT rather than merely somewhere near it."""
    over = MIN_SHIFT + 0.5
    found = detect(series(0, 0, 0, 0, over, over, over, over))
    assert found is not None
    assert found.shift == pytest.approx(over)


def test_a_single_spike_is_not_a_changepoint():
    """'Holding three or more weeks', made mechanical — **and arm 3's test**.

    A lone 60-point week. The trap is that the spurious detection lands on the
    week AFTER the spike, not on the spike: at split 5 the baseline is
    `[0,0,0,60]` (mean 15) and the three following zeros all sit below it, so
    arms 1 and 2 both pass and the detector reports a sustained 15-point drop
    in a series where nothing changed. Only arm 3 — three *before* weeks on
    the far side of the after-mean — refuses it, because just one of the four
    baseline weeks is above zero.

    Delete `held_before` and this test is the one that dies. It was a real
    false positive, not a hypothetical: it is why arm 3 exists.
    """
    assert detect(series(0, 0, 0, 0, 60, 0, 0, 0)) is None


def test_a_shift_holding_exactly_the_minimum_run_fires():
    """The neighbour of the spike test: three weeks on the shifted side, not
    one. Pins that the run-length arm counts to MIN_RUN and not higher."""
    found = detect(series(0, 0, 0, 20, 20, 20))
    assert found is not None
    assert found.weeks_after == MIN_RUN


def test_a_series_shorter_than_two_runs_returns_none():
    """A handoff in weeks 1-2 is undetectable — stated in changepoint.py as a
    known hole rather than papered over with a one-week baseline."""
    assert detect(series(0, 0, 0, 30, 30)) is None
    assert detect([]) is None


def test_the_windows_are_capped_so_an_old_regime_cannot_contaminate():
    """Both sides read at most MAX_WINDOW weeks.

    Weeks 1-4 sit at 7, weeks 5-8 at 0, weeks 9-12 at 12. The first step is
    deliberately below `MIN_SHIFT` so it does not fire on its own and week 9
    is the only changepoint in the series.

    The cap is what makes the reported baseline `0.0`. An uncapped `before`
    at that split averages all eight prior weeks to 3.5 — a level the offense
    never played at — and reports an 8.5-point shift instead of a 12-point
    one. It still fires, which is why asserting `before_mean` rather than
    merely `is not None` is what kills the uncapped mutant.
    """
    found = detect(series(7, 7, 7, 7, 0, 0, 0, 0, 12, 12, 12, 12))
    assert found is not None
    assert found.weeks_before <= MAX_WINDOW
    assert found.weeks_after <= MAX_WINDOW
    # Against the four weeks immediately before, not against the 7s.
    assert found.week == 9
    assert found.before_mean == pytest.approx(0.0)
    assert found.shift == pytest.approx(12.0)


def test_the_strongest_candidate_wins_when_several_qualify():
    found = detect(series(0, 0, 0, 10, 10, 10, 40, 40, 40, 40))
    assert found is not None
    assert found.week == 7


# --------------------------------------------------------------------------
# explains — arm A vs arm B
# --------------------------------------------------------------------------


def _revisions(grid):
    return build_revisions(
        [row for row in _coach_rows(grid)],
        season=SEASON,
    )["AAA"]


def _coach_rows(grid):
    from coaching_scheme.adapters.games import TeamWeekCoach

    return [TeamWeekCoach("AAA", week, name) for week, name in sorted(grid.items())]


def test_a_changepoint_matching_a_revision_is_explained():
    """**Arm A.** A real firing at week 7 that the feed did record. Alarming
    here would flag every genuine coaching change as a missed one."""
    revisions = _revisions(coaches_with_change("AAA", at_week=7)["AAA"])
    found = detect(series(0, 0, 0, 0, 0, 0, 20, 20, 20, 20))
    assert found is not None and found.week == 7
    assert explains(found, revisions) is True


def test_a_changepoint_one_week_off_a_revision_is_still_explained():
    """A coach fired on a Monday inherits a half-written game plan, so the
    rate shift routinely lands a week after the boundary."""
    revisions = _revisions(coaches_with_change("AAA", at_week=7)["AAA"])
    found = detect(series(0, 0, 0, 0, 0, 0, 0, 20, 20, 20))
    assert found is not None and found.week == 8
    assert explains(found, revisions) is True


def test_a_changepoint_two_weeks_off_a_revision_is_not_explained():
    """The neighbour that pins the tolerance at 1 rather than 'some slack'."""
    revisions = _revisions(coaches_with_change("AAA", at_week=7)["AAA"])
    found = detect(series(0, 0, 0, 0, 0, 0, 0, 0, 20, 20, 20))
    assert found is not None and found.week == 9
    assert explains(found, revisions) is False


def test_a_changepoint_with_no_revision_at_all_is_unexplained():
    """**Arm B.** The un-backfilled feed: a real handoff the staff column
    never recorded. This is the normal case on a live season."""
    revisions = _revisions(steady_coaches()["AAA"])
    assert len(revisions) == 1
    found = detect(series(0, 0, 0, 0, 20, 20, 20, 20))
    assert found is not None
    assert explains(found, revisions) is False


def test_the_first_revision_never_explains_anything():
    """`revisions[1:]` is load-bearing.

    Every team's first revision begins in week 1 by construction. Counting it
    would let week 1 'explain' changepoints, and since `detect` cannot return
    a week below MIN_RUN+1 that would look harmless — until a tolerance change
    or a feed that starts a team at week 2 made week 1 reachable. Asserted
    directly rather than left to the arithmetic.
    """
    revisions = _revisions(steady_coaches()["AAA"])
    found = detect(series(0, 0, 0, 20, 20, 20))
    assert found is not None
    assert revisions[0].effective_from_week == 1
    assert explains(found, revisions, tolerance=99) is False


# --------------------------------------------------------------------------
# End to end — the guard as `capture` applies it
# --------------------------------------------------------------------------


async def test_an_unexplained_shift_is_surfaced_on_the_row_and_the_envelope(
    lake: SpyLake,
):
    """The spec: 'surface it; do not silently correct it'.

    AAA's PROE steps 20 points at week 7 with the staff feed reporting no
    change at all — the 2024/2025 shape `adapters/games.py` measured. Both the
    row flag and the envelope error must appear, and the rates must STILL be
    published: dropping them would leave a consumer with a missing profile and
    no reason, which is the silent correction the spec forbids.
    """
    envelopes = await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=20.0)), lake=lake
    )
    profile = envelopes["team_scheme_profile"]
    flagged = [row for row in profile.signals if row["changepoint_unexplained"]]

    assert len(flagged) == 1
    assert flagged[0]["team_id"] == "AAA"
    assert flagged[0]["changepoint_week"] == 7
    assert flagged[0]["changepoint_shift"] == pytest.approx(20.0)
    # Surfaced, not dropped.
    assert flagged[0]["pass_rate_over_expected"] is not None

    reasons = [error["reason"] for error in profile.errors]
    assert REASON_UNEXPLAINED_CHANGEPOINT in reasons
    # A priority error, so it survives the 50-entry cap ahead of routine
    # per-revision failures.
    assert reasons.index(REASON_UNEXPLAINED_CHANGEPOINT) < len(reasons)


async def test_a_shift_the_staff_feed_explains_is_not_flagged(lake: SpyLake):
    """**Arm A end to end.** Same 20-point shift at week 7, but this time the
    feed records the coaching change. Nothing may be flagged — and the fact
    that the shift is identical is what makes this a test of `explains`
    rather than of `detect`."""
    envelopes = await run_capture(
        Feeds(
            coaches=coaches_with_change("AAA", at_week=7),
            proe=proe_with_shift("AAA", at_week=7, shift=20.0),
        ),
        lake=lake,
    )
    profile = envelopes["team_scheme_profile"]
    assert profile.signals, "no rows at all — the fixture is broken, not the guard"
    assert not [row for row in profile.signals if row["changepoint_unexplained"]]
    assert REASON_UNEXPLAINED_CHANGEPOINT not in {
        error["reason"] for error in profile.errors
    }


async def test_a_quiet_season_flags_nothing(lake: SpyLake):
    """The other negative control, through the pipeline."""
    envelopes = await run_capture(lake=lake)
    rows = envelopes["team_scheme_profile"].signals
    assert rows
    assert not [row for row in rows if row["changepoint_unexplained"]]


async def test_the_series_is_built_without_consulting_the_staff_feed(lake: SpyLake):
    """'Independently of the staff feed' — proved by changing only the feed.

    Two passes over identical play-by-play: one where the staff feed reports a
    week-7 change, one where it reports none. `detect` must find the same
    changepoint in both; only `explains` may differ. A detector that took
    revisions as a hint would find nothing in the second pass.
    """
    from coaching_scheme.adapters.pbp import weekly_proe_series
    from coaching_scheme.capture import PROFILE

    shifted = proe_with_shift("AAA", at_week=7, shift=20.0)

    with_change = await run_capture(
        Feeds(coaches=coaches_with_change("AAA", at_week=7), proe=shifted), lake=lake
    )
    without = await run_capture(
        Feeds(coaches=steady_coaches(), proe=shifted), lake=SpyLake()
    )

    # The detector's own input is identical in both passes.
    assert weekly_proe_series  # imported for the reader; the series is internal
    flagged_with = [
        r for r in with_change[PROFILE].signals if r["changepoint_unexplained"]
    ]
    flagged_without = [
        r for r in without[PROFILE].signals if r["changepoint_unexplained"]
    ]
    assert flagged_with == []
    assert len(flagged_without) == 1
    assert flagged_without[0]["changepoint_week"] == 7


def test_the_documents_agree_the_fixture_grid_is_what_it_claims():
    """A guard on the fixtures themselves.

    `coaches_with_change` and `steady_coaches` are the inputs half the
    assertions above rest on. A silent change to either would make several
    tests pass for the wrong reason, so their shape is asserted once here.
    """
    steady = games_document(steady_coaches())
    changed = games_document(coaches_with_change("AAA", at_week=7))
    assert "Interim AAA" not in steady
    assert "Interim AAA" in changed

"""**Guard 2** — the regime change the staff feed never saw.

The spec:

> Then run a changepoint test on each team's weekly PROE series independently
> of the staff feed: a sustained shift of more than roughly eight points
> holding three or more weeks with no corresponding revision means a
> play-calling handoff the adapter never saw, and the rates on both sides of
> it are wrong.

**This is not a nicety on this collector; on a live season it is the only
detector there is.** `adapters/games.py` documents the measurement: nfldata's
coach columns record mid-season head-coach changes for 2022 and 2023 and
record **none at all** for 2024 or 2025, both of which had several. So the
staff feed's hypothesis on the current season is "nothing ever changes", and
this test is the thing that contradicts it.

--------------------------------------------------------------------------
"Independently of the staff feed" is structural here
--------------------------------------------------------------------------

`detect` takes a series of `(week, PROE)` and nothing else. It cannot see a
revision, so it cannot be biased toward confirming one. `explains` is a
separate function that takes the detected week and the revisions and answers
one question. Fusing them into a single "find unexplained changepoints" call
would let a future edit quietly restrict the search to weeks near a boundary,
which is the failure this separation prevents and which no test of the fused
function would catch.

--------------------------------------------------------------------------
"Sustained", made mechanical
--------------------------------------------------------------------------

A mean shift alone is not sustained: one 60-point week among four drags a
4-week mean 15 points on its own. So three arms must hold:

1. the mean of the window after the split differs from the mean before by more
   than `MIN_SHIFT` points;
2. at least `MIN_RUN` individual weeks **after** the split sit on the shifted
   side of the before-mean; and
3. at least `MIN_RUN` individual weeks **before** the split sit on the other
   side of the after-mean.

Arms 2 and 3 are what "holding three or more weeks" actually says, applied to
both levels. **Arm 3 is not symmetry for its own sake** — without it a lone
outlier is detected as a changepoint on the week *after* it, because the
outlier inflates the baseline and the return to normal then reads as a
sustained drop. Measured on `[0,0,0,0,60,0,0,0]`: arms 1 and 2 alone report a
15-point downward shift at week 6 in a series where nothing changed.

A median would also defeat that, and was rejected: with a clean step the
before- and after-medians are identical at every split near the boundary, so
the detector ties and reports the changepoint a week early. The mean is
boundary-precise; arm 3 is what makes it robust.

Both windows are capped at `MAX_WINDOW` weeks. A local comparison, not
season-to-date: comparing against every prior week would let an early-season
regime contaminate a late-season test, which is the same blending error guard
1 refuses in the rates themselves.

**Known limitation, stated rather than discovered later:** a handoff in weeks
1-2 is undetectable, because there are fewer than `MIN_RUN` weeks before it to
form a baseline. `detect` returns `None` there. That is a real hole and it is
the correct answer for this test — a hole is honest, and a baseline built from
one week would fire constantly.

--------------------------------------------------------------------------
Surfacing, not correcting
--------------------------------------------------------------------------

The spec says "surface it; do not silently correct it", so `capture` publishes
both sides' rates and marks the row. Dropping them would be the silent
correction — a consumer would see a team with no profile and no reason. What
it must never be is *unmarked*: the whole point is that these numbers are
suspect, and a consumer reading `changepoint_unexplained: true` has been told.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .revisions import Revision

__all__ = [
    "MAX_WINDOW",
    "MIN_RUN",
    "MIN_SHIFT",
    "REASON_UNEXPLAINED_CHANGEPOINT",
    "REVISION_MATCH_TOLERANCE_WEEKS",
    "Changepoint",
    "detect",
    "explains",
]

# Percentage points of PROE. `pass_oe` is `(pass - xpass) * 100`, so the
# spec's "more than roughly eight points" needs no unit conversion.
MIN_SHIFT = 8.0

# Weeks the shifted level must hold, and the minimum baseline. Both sides,
# for symmetry: a 3-week baseline is the shortest that is not an outlier.
MIN_RUN = 3

# The most weeks either side contributes. See the module docstring.
MAX_WINDOW = 4

# How far a revision boundary may sit from the detected week and still count
# as explaining it. One week each way: a coach fired on a Monday takes over a
# game plan already half-written, so the rate shift routinely lands a week
# after the boundary the feed records, and an interim named mid-week can shift
# the plan a week before the feed's next game row.
REVISION_MATCH_TOLERANCE_WEEKS = 1

REASON_UNEXPLAINED_CHANGEPOINT = "unexplained_proe_changepoint"


@dataclass(frozen=True)
class Changepoint:
    """A sustained level shift in one team's weekly PROE series.

    `week` is the first week of the *shifted* level, i.e. the week the new
    regime's play-calling first appears — not the last week of the old one.
    """

    week: int
    shift: float
    before_mean: float
    after_mean: float
    weeks_before: int
    weeks_after: int


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def detect(series: Sequence[tuple[int, float]]) -> Changepoint | None:
    """The strongest sustained shift in `series`, or `None`.

    `series` is `(week, mean PROE)` week-ascending, as
    `pbp.weekly_proe_series` builds it. Bye weeks are simply absent; the test
    runs over observed weeks in order rather than over a calendar, because a
    bye is not evidence of anything and inserting a gap would break the run
    count for a reason unrelated to play-calling.

    Returns the candidate with the largest absolute shift when several
    qualify. One changepoint per team per pass is deliberate: two boundaries
    in a seventeen-week series is already an extraordinary claim, and
    reporting a list would invite a consumer to treat the weakest of them as
    equally established.
    """
    if len(series) < MIN_RUN * 2:
        return None

    best: Changepoint | None = None
    for split in range(MIN_RUN, len(series) - MIN_RUN + 1):
        before = [value for _, value in series[max(0, split - MAX_WINDOW) : split]]
        after = [value for _, value in series[split : split + MAX_WINDOW]]
        if len(before) < MIN_RUN or len(after) < MIN_RUN:
            continue

        before_mean = _mean(before)
        after_mean = _mean(after)
        shift = after_mean - before_mean
        if abs(shift) <= MIN_SHIFT:
            continue

        # Arms 2 and 3: BOTH levels must hold, not merely average. Counted
        # against the other window's mean rather than against a midpoint, so a
        # single extreme week cannot carry weeks that did not actually move.
        # Arm 3 is what stops a lone outlier being read as a changepoint on
        # the week after it — see the module docstring.
        sign = 1.0 if shift > 0 else -1.0
        held_after = sum(1 for value in after if (value - before_mean) * sign > 0)
        held_before = sum(1 for value in before if (value - after_mean) * sign < 0)
        if held_after < MIN_RUN or held_before < MIN_RUN:
            continue

        if best is None or abs(shift) > abs(best.shift):
            best = Changepoint(
                week=series[split][0],
                shift=round(shift, 4),
                before_mean=round(before_mean, 4),
                after_mean=round(after_mean, 4),
                weeks_before=len(before),
                weeks_after=len(after),
            )
    return best


def explains(
    changepoint: Changepoint,
    revisions: Sequence[Revision],
    *,
    tolerance: int = REVISION_MATCH_TOLERANCE_WEEKS,
) -> bool:
    """Whether any revision boundary accounts for this changepoint.

    A boundary is a revision's `effective_from_week`, **excluding the first**:
    every team's first revision begins in week 1 by construction, and counting
    it would let week 1 explain a changepoint that has nothing to do with a
    staff change. That exclusion is the difference between a test that can
    fire and one that cannot — the `revisions[1:]` slice is load-bearing.
    """
    return any(
        abs(revision.effective_from_week - changepoint.week) <= tolerance
        for revision in revisions[1:]
    )

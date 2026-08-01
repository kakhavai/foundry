"""**Guard 2 — and it does not work. It ships DISABLED. Read this first.**

The spec asks for it:

> Then run a changepoint test on each team's weekly PROE series independently
> of the staff feed: a sustained shift of more than roughly eight points
> holding three or more weeks with no corresponding revision means a
> play-calling handoff the adapter never saw, and the rates on both sides of
> it are wrong.

That test was built, measured against five live seasons, and **found to have
no discriminating power whatsoever**. `CHANGEPOINT_ENABLED` is `False`, and
every row publishes `changepoint_unexplained: null` with a reason rather than
`false` — because `false` would assert "we checked and there is no
changepoint", which is a claim this collector cannot support.

--------------------------------------------------------------------------
What was measured, and in what order
--------------------------------------------------------------------------

All figures from live nflverse play-by-play 2021-2025 (160 team-seasons, 2,718
team-weeks) read through this service's own adapter. The **null** is the same
real series with its weeks randomly permuted, so no changepoint exists in it by
construction and every firing is a false positive.

**1. The spec's own rule, as originally implemented** — a >8-point shift in a
difference of two <=4-week means, with run-length arms on both sides:

| | real | shuffled null |
|---|---|---|
| firing rate | **65.0%** (104/160) | **55.4%** |

Two thirds of the league flagged every season, at a rate barely above pure
noise. A threshold sweep separates them nowhere:

| `MIN_SHIFT` | real | null | recall |
|---|---|---|---|
| 8 | 65.0% | 55.4% | 2/13 |
| 10 | 38.8% | 31.7% | 2/13 |
| 12 | 19.4% | 15.4% | 2/13 |
| 14 | 7.5% | 7.3% | 1/13 |
| 16 | 3.8% | 3.4% | 1/13 |
| 18 | 1.9% | 1.4% | 0/13 |

**2. A better estimator, properly calibrated.** The mean-difference statistic
was replaced by the pooled-variance max-t below, and the fixed threshold by a
**per-team permutation test** — the team's own series shuffled B times, the
same max-t computed on each, and the observed value compared against its own
null. That controls the false-positive rate by construction, and it does:

| alpha | real firing | measured FPR | recall |
|---|---|---|---|
| 0.05 | 6.2% | **5.0%** | 0/13 |
| 0.02 | 2.5% | **2.2%** | 0/13 |
| 0.01 | 1.2% | **1.1%** | 0/13 |

The machinery works exactly as advertised — the FPR lands on alpha at every
level. And the real firing rate lands on the null firing rate too. **There is
nothing to find.**

**3. The ceiling on any possible detector.** Hand one the true changepoint week
for free — no search, no multiple-comparisons penalty — and ask how big the
effect is:

    mean |shift| at a REAL coaching change     4.18 points   mean |t| 1.05
    mean |shift| at a RANDOM week, same teams  3.92 points   mean |t| 1.02
    within-team weekly PROE sd                 6.89 points
    nominally significant (|t| >= 2)           2 of 13

A real coaching change is **indistinguishable from an arbitrary week**. Three
of the motivating cases barely move: NYJ 2024 +0.16, TEN 2025 -0.16, CHI 2024
+1.11. NO 2024 moves +2.98, the opposite direction. The two that do move
(CAR 2022 -14.73, CAR 2023 -9.95) are the same franchise twice.

**So the fault is not the threshold, the estimator or the calibration. The
signal is not in this statistic.** No test on weekly team PROE can do this
job, because a regime change is ~0.6 sd of the week-to-week noise it must be
seen through. The spec's "can move ten points in a week" is a true statement
about a *week* — sd is 8.1 points, so ten-point swings are routine — and that
is exactly what buries a four-point shift in the *regime*.

--------------------------------------------------------------------------
Why this is shipped disabled rather than deleted
--------------------------------------------------------------------------

Publishing it would be worse than useless. This collector reports one revision
per team on any current season (see `adapters/games.py`), so `explains()`
returns `False` for every candidate and **every firing publishes as
unexplained** — roughly twenty rows a pass marked "the rates on BOTH sides are
suspect", and `coaching_scheme_unexplained_changepoints` pinned near twenty
forever. That is the always-red-gauge pathology this collector's own
play-caller argument rejects, arriving through the guard the design rests on.

It is kept rather than deleted because the *calibration machinery* is correct
and reusable, and because the measurement is the point: a future statistic with
real power needs exactly this permutation harness to prove itself, and deleting
it would throw away the evidence that the obvious approach fails.

**The follow-up, stated so it is not rediscovered:** weekly PROE may simply be
the wrong series. `neutral_pass_rate`, `personnel_rates` or
`sec_per_play_neutral` may carry a sharper regime signal, and the oracle test
above is the cheap way to check before building anything — if the true-week
effect does not clearly exceed the random-week effect on a candidate series,
stop. That test was **not** run on the other series here: swapping the spec's
named statistic is a design change, not a bug fix.

--------------------------------------------------------------------------
Determinism, which matters more than it looks
--------------------------------------------------------------------------

A permutation p-value is a random quantity. Published on a row, one that moved
between passes over identical upstream data would make every daily digest
unique and **silently disable the unchanged-snapshot gate** — the lake would
grow an object a day forever. So the seed comes from
`(collector, season, team)` through `hashlib.blake2b`, never `hash()`: Python
salts `hash()` on `str` per process, so two pods would draw different nulls for
one team and a single pod would disagree with itself across a restart.
"""

import hashlib
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from .revisions import Revision

__all__ = [
    "CHANGEPOINT_ENABLED",
    "CHANGEPOINT_UNCALIBRATED",
    "MAX_WINDOW",
    "MIN_RUN",
    "PERMUTATIONS",
    "REASON_UNEXPLAINED_CHANGEPOINT",
    "REVISION_MATCH_TOLERANCE_WEEKS",
    "SIGNIFICANCE_ALPHA",
    "Changepoint",
    "detect",
    "explains",
    "max_t",
    "permutation_seed",
]

# **The guard is off.** See the module docstring for the five seasons of
# measurement behind this line. Flipping it to True without first replacing the
# underlying series republishes a detector whose measured recall is 0/13 and
# whose firing rate equals its own noise floor.
CHANGEPOINT_ENABLED = False

# Stamped on every row while the guard is off. A row publishes
# `changepoint_unexplained: null`, never `false`: `false` asserts "checked, and
# clean", and this collector has not checked.
CHANGEPOINT_UNCALIBRATED = (
    "changepoint_detector_disabled_no_discriminating_power_on_weekly_proe"
)

# Weeks the shifted level must hold, and the minimum baseline. Both sides.
MIN_RUN = 3

# The most weeks either side contributes. A local comparison, not
# season-to-date: comparing against every prior week lets an early-season
# regime contaminate a late-season test, which is the same blending error
# guard 1 refuses in the rates themselves.
MAX_WINDOW = 4

# Permutations per team for the null. 299 puts the smallest reachable p-value
# at 1/300 = 0.0033, comfortably below the alpha below.
PERMUTATIONS = 299

# The false-positive rate this test targets. **Measured, not hoped for:** 1.1%
# against an 800-series shuffled null at this alpha. A permutation test
# delivers its alpha by construction; the measurement is the check that the
# implementation does what the theory says.
SIGNIFICANCE_ALPHA = 0.01

# How far a revision boundary may sit from the detected week and still count as
# explaining it. One week each way: a coach fired on a Monday inherits a game
# plan already half-written.
REVISION_MATCH_TOLERANCE_WEEKS = 1

REASON_UNEXPLAINED_CHANGEPOINT = "unexplained_proe_changepoint"


@dataclass(frozen=True)
class Changepoint:
    """A level shift in one team's weekly PROE series, with its p-value.

    `week` is the first week of the *shifted* level — the week a new regime's
    play-calling would first appear, not the last week of the old one.

    `p_value` is against the team's **own** permuted series, so it adapts to
    that team's volatility rather than to a league-wide constant. A team whose
    PROE barely moves needs a smaller shift to be believed than one swinging
    twenty points a week, and a fixed threshold cannot express that.
    """

    week: int
    shift: float
    p_value: float
    statistic: float
    before_mean: float
    after_mean: float
    weeks_before: int
    weeks_after: int


def max_t(
    values: Sequence[float],
) -> tuple[float, int | None, float, float, float, int, int]:
    """The largest pooled-variance |t| over every admissible split.

    Returns `(t, split_index, shift, before_mean, after_mean, n_before,
    n_after)`, with `split_index` `None` when no split is admissible.

    A **t**, not a raw mean difference, and that is the estimator fix: it
    normalises the shift by the team's own within-window scatter, so the same
    six points mean different things for a steady offence and a volatile one.
    The raw difference the first implementation used treated them identically,
    which is half of why it fired on two thirds of the league.
    """
    best: tuple[float, int | None, float, float, float, int, int] = (
        0.0,
        None,
        0.0,
        0.0,
        0.0,
        0,
        0,
    )
    n = len(values)
    for split in range(MIN_RUN, n - MIN_RUN + 1):
        before = values[max(0, split - MAX_WINDOW) : split]
        after = values[split : split + MAX_WINDOW]
        n1, n2 = len(before), len(after)
        if n1 < MIN_RUN or n2 < MIN_RUN:
            continue
        before_mean = statistics.fmean(before)
        after_mean = statistics.fmean(after)
        residuals = sum((v - before_mean) ** 2 for v in before) + sum(
            (v - after_mean) ** 2 for v in after
        )
        dof = n1 + n2 - 2
        if dof <= 0:
            continue
        pooled = residuals / dof
        if pooled <= 1e-9:
            # Two perfectly flat windows. `t` would be infinite for any non-zero
            # shift, which is an artefact of a degenerate sample rather than
            # evidence — synthetic fixtures hit this constantly, real data
            # never does.
            continue
        statistic = (
            abs(after_mean - before_mean) / (pooled * (1.0 / n1 + 1.0 / n2)) ** 0.5
        )
        if statistic > best[0]:
            best = (
                statistic,
                split,
                after_mean - before_mean,
                before_mean,
                after_mean,
                n1,
                n2,
            )
    return best


def permutation_seed(collector: str, season: int, team_id: str) -> int:
    """A stable seed for one team-season's null.

    `hashlib.blake2b`, **never `hash()`**: Python salts `hash()` on `str` per
    process, so two pods would draw different nulls for the same team and one
    pod would disagree with itself across a restart. A p-value that moves
    between passes over identical upstream data makes every digest unique and
    silently disables the unchanged-snapshot gate.
    """
    key = f"{collector}:{season}:{team_id}".encode()
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big")


def detect(
    series: Sequence[tuple[int, float]],
    *,
    seed: int,
    alpha: float = SIGNIFICANCE_ALPHA,
    permutations: int = PERMUTATIONS,
) -> Changepoint | None:
    """The strongest level shift in `series`, if it beats the team's own null.

    `series` is `(week, mean PROE)` week-ascending, as
    `pbp.weekly_proe_series` builds it. It takes **no revisions**, so it cannot
    be biased toward confirming one — `explains` is separate for that reason,
    and fusing them would let a future edit quietly restrict the search to
    weeks near a boundary.

    Bye weeks are simply absent; the test runs over observed weeks in order
    rather than over a calendar, because a bye is not evidence of anything.

    **Known limitation:** a handoff in weeks 1-2 is undetectable — fewer than
    `MIN_RUN` weeks precede it, so no baseline exists. `None` is returned
    rather than a verdict built on a one-week baseline.

    This function is correct and is **not called in production**. See the
    module docstring: `CHANGEPOINT_ENABLED` is `False` because the *series*
    carries no signal, not because the test is wrong.
    """
    if len(series) < MIN_RUN * 2:
        return None

    values = [value for _, value in series]
    statistic, split, shift, before_mean, after_mean, n1, n2 = max_t(values)
    if split is None:
        return None

    # The team's own null: the same weeks, reordered. Under the hypothesis that
    # no changepoint exists, week order carries no information, so the permuted
    # maxima are draws from the distribution the observed maximum came from —
    # including the multiple-comparisons inflation from maximising over ~11
    # splits, which a fixed threshold ignores entirely.
    rng = random.Random(seed)
    shuffled = list(values)
    at_least_as_extreme = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        null_statistic, null_split = max_t(shuffled)[:2]
        if null_split is not None and null_statistic >= statistic:
            at_least_as_extreme += 1

    # The +1s are not a fudge: including the observed value in its own
    # reference set is what keeps the test exact rather than anti-conservative,
    # and it is why `p` can never be reported as 0.
    p_value = (1 + at_least_as_extreme) / (permutations + 1)
    if p_value > alpha:
        return None

    return Changepoint(
        week=series[split][0],
        shift=round(shift, 4),
        p_value=round(p_value, 6),
        statistic=round(statistic, 4),
        before_mean=round(before_mean, 4),
        after_mean=round(after_mean, 4),
        weeks_before=n1,
        weeks_after=n2,
    )


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
    staff change. The `revisions[1:]` slice is load-bearing.

    **On a current season this returns `False` for everything**, because
    nfldata's coach columns carry one revision per team — see
    `adapters/games.py`. That is not a defect here; it is the reason a working
    detector would matter, and the reason a broken one is so expensive.
    """
    return any(
        abs(revision.effective_from_week - changepoint.week) <= tolerance
        for revision in revisions[1:]
    )

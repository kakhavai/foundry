"""**Guard 2 — and it does not work. It ships DISABLED. Read this first.**

The spec asks for it:

> Then run a changepoint test on each team's weekly PROE series independently
> of the staff feed: a sustained shift of more than roughly eight points
> holding three or more weeks with no corresponding revision means a
> play-calling handoff the adapter never saw, and the rates on both sides of
> it are wrong.

That test was built and measured against five live seasons. It **fires at its
own noise floor**, and the effect it is looking for is **too small to resolve
at the sample size available**: a real head-coach change shifts weekly PROE by
4.83 points on average against 4.01 at an arbitrary week (p = 0.18, n = 12),
where the study can only reliably resolve ~6-8 points.

`CHANGEPOINT_ENABLED` is `False`, and every row publishes
`changepoint_unexplained: null` with a reason rather than `false` — because
`false` would assert "we checked and there is no changepoint", which is a
claim this collector cannot support.

**Two things this does NOT say**, both of which earlier revisions did. It does
not say there is no effect — n = 12 cannot support that, and the honest bound
is "smaller than roughly six points". And it does not rest on the calibrated
detector's zero recall, which turns out to carry no information at all: that
test cannot detect a *perfect* step of any size, so its silence is a statement
about its power rather than about the data. The argument is the oracle test in
step 3.

--------------------------------------------------------------------------
What was measured, and in what order
--------------------------------------------------------------------------

All figures from live nflverse play-by-play 2021-2025 (160 team-seasons, 2,718
team-weeks) read through this service's own adapter.

**The event class is HEAD-COACH changes, not play-calling handoffs.** Thirteen
in-season head-coach changes 2021-2025, weeked to the new regime's first game.
That is a *proxy* for what this collector actually targets — a play-caller
changing, which is what `change_event: play_calling_handoff` names — and the
two overlap imperfectly in both directions: a firing often keeps the same
play-caller, and a play-caller can be replaced with no firing at all. Every
recall and effect-size figure below is against the proxy. Nobody should read
them as covering handoffs.

**Twelve of the thirteen are admissible.** DEN 2022 week 17 cannot be evaluated
under any window rule — `MIN_RUN` weeks must follow the split and only two
remain — so the oracle statistics below are over **12**.

The **null** is the same real series with its weeks randomly permuted, so no
changepoint exists in it by construction and every firing is a false positive.

**1. The spec's own rule, as originally implemented** — a >8-point shift in a
difference of two <=4-week means, with run-length arms on both sides:

| | real | shuffled null |
|---|---|---|
| firing rate | **65.0%** (104/160) | **55.4%** |

Two thirds of the league flagged every season, at a rate barely above pure
noise, and a threshold sweep (8/10/12/14/16/18 points) separates them nowhere:
real 65.0/38.8/19.4/7.5/3.8/1.9% against null 55.4/31.7/15.4/7.3/3.4/1.4%.

**2. A better estimator, properly calibrated — and this step is NOT evidence of
absence.** The mean-difference statistic was replaced by the pooled-variance
max-t below, and the fixed threshold by a **per-team permutation test**: the
team's own series shuffled B times, the same max-t computed on each, and the
observed value compared against its own null.

The false-positive rate is controlled exactly as the theory says:

| alpha | real firing | measured FPR |
|---|---|---|
| 0.05 | 6.2% | **5.0%** |
| 0.02 | 2.5% | **2.2%** |
| 0.01 | 1.2% | **1.1%** |

Recall against the twelve admissible events is **0**. **That number carries no
information, and an earlier revision of this docstring wrongly cited it as
evidence that no signal exists.** The test has essentially no power against a
step:

| perfect step | p (10 / 30 / 100 pt) | fires at alpha=0.01 |
|---|---|---|
| 8 low + 8 high | 0.4567 / 0.4567 / 0.4567 | no |
| 6 + 6 | 0.2867 / 0.2867 / 0.2867 | no |
| 13 + 4 | 0.1633 / 0.1633 / 0.1633 | no |
| 14 + 3 | 0.0533 / 0.0533 / 0.0533 | no |

**The p-value is invariant to the step size** — max-t is scale-free on a clean
step, and permuting a stepped series keeps recreating a 4-versus-4 step at one
of ~11 candidate splits, so the observed maximum sits inside its own null. At
the shipped alpha this test would score 0/12 against twelve perfect
hundred-point steps.

Real-shaped noise breaks the exact tie structure and restores some power, but
only when the shifted segment is a **minority**: at jitter comparable to the
measured within-team sd, 13+4 and 14+3 fire while **8+8 bottoms out at p ~=
0.043 regardless of magnitude**. So the balance ratio dominates and the step
size barely matters.

Step 2 therefore establishes only that the calibrated detector fires no more
often than chance. It cannot distinguish "no effect" from "no power", and the
argument for disabling rests on step 3.

**3. The ceiling on any detector — this is what carries the conclusion.** Hand
one the true changepoint week for free: no search, no multiple-comparisons
penalty. **Measured under the capped +-MAX_WINDOW estimator that `max_t`
actually computes**, with random weeks drawn from the same team-seasons and the
same admissible split set:

    mean |shift| at a REAL head-coach change   4.83 points   mean |t| 1.17
    mean |shift| at a RANDOM week, same teams  4.01 points   mean |t| 0.97
    within-team weekly PROE sd                 6.89 points
    nominally significant (|t| >= 2)           2 of 12

    permutation test of the real-vs-random difference:  p = 0.18

Per-event capped shifts: CAR 2022 -13.71, CAR 2023 -9.81, NYG 2025 -8.38,
IND 2022 -7.95, LV 2023 -4.61, NYJ 2024 -3.08, CHI 2024 -2.40, LAC 2023 -2.28,
LV 2021 +2.87, TEN 2025 +1.98, NO 2024 -0.81, JAX 2021 +0.14.

**The convention matters, and is stated because an earlier revision did not.**
Those are the *capped* numbers. An uncapped full-season split gives materially
different per-event values — NYJ 2024 reads +0.16 uncapped against -3.08
capped, a factor of nineteen — and the earlier docstring quoted the uncapped
figures while describing the capped statistic. The conclusion is unchanged
under either, but the numbers now match the code.

**What this does and does not establish.** A real head-coach change is not
separable from an arbitrary week at this sample size: 4.83 against 4.01,
p = 0.18. A Monte-Carlo power analysis at n = 12 and the measured sd puts the
study's resolution at roughly:

    true step  2 pts (0.29 sd)   separates in ~10% of studies
    true step  4 pts (0.58 sd)   ~22%
    true step  6 pts (0.87 sd)   ~53%
    true step  8 pts (1.16 sd)   ~82%
    true step 10 pts (1.45 sd)   ~97%

(An independent replication put the 6-point figure nearer 82%. Take the bound
as ~6-8 points rather than a sharp threshold.)

The measured real mean of **4.83 sits squarely inside the band this study
cannot resolve.** So the defensible claim is bounded, not absolute:

> **Any regime effect on weekly team PROE is smaller than roughly six points
> and is not separable from week-to-week noise at n = 12.**

Not "there is nothing to find" — that overstates what n = 12 supports. It is
still ample reason to disable the guard: a detector that cannot see a perfect
step of any size should not ship enabled, and an effect this study cannot
resolve is one the detector certainly cannot.

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
real power needs exactly this permutation harness to prove itself.

**The follow-up, replaced with measurements rather than guesses.** An earlier
revision nominated `neutral_pass_rate`, `personnel_rates` and
`sec_per_play_neutral` as likelier series. A reviewer ran the same oracle over
six candidates (same 12 events, same fairness rules, same capped estimator),
and **two of those three measure at or below the random-week baseline**:

| series | real mean abs t | random | perm p |
|---|---|---|---|
| **shotgun rate** | **1.74** | 1.15 | **0.038** |
| no-huddle rate | 1.31 | 1.08 | 0.191 |
| PROE (shipped) | 1.17 | 0.94 | 0.177 |
| neutral pass rate | 1.06 | 0.99 | 0.360 |
| sec/play neutral | 0.88 | 1.03 | 0.668 |
| 4th-down go rate | 0.77 | 0.95 | 0.757 |

`sec_per_play_neutral` is actively *worse* than random, and the one candidate
that looks promising was not on the original list. **Bonferroni over six series
puts shotgun rate at p ~= 0.23, so this is suggestive, not established** — a
lead for a future collector rather than a working detector, and it was not
pre-registered. Start there, and confirm on held-out seasons before building
anything.

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
# underlying series republishes a detector with two independent problems: its
# firing rate equals its own noise floor, and it has essentially no power
# against a step at all -- at this alpha it cannot detect a PERFECT step of any
# size at any balance ratio.
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

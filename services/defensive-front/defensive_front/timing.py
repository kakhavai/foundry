"""The timing-confound guard — the spec's named failure mode, made testable.

The failure, in the spec's words:

    Pressure rate is jointly produced by the front and the offense's time to
    throw, so a front that draws quick-game and screen-heavy opponents posts a
    depressed `pressure_rate_generated` that survives naive opponent
    adjustment, because the adjustment corrects for line quality and not for
    release timing.

and the assertion it asks for:

    regress `pressure_rate_generated_adj` on `mean_time_to_throw_faced` across
    the 32 teams and require the residual slope to be statistically
    indistinguishable from zero; a non-zero slope means the adjustment model
    is missing the timing term entirely.

--------------------------------------------------------------------------
Measured before it was trusted, on the real 2025 regular season
--------------------------------------------------------------------------

A guard nobody has run against a null is a decoration. Three numbers, all
produced through this exact code path (see README.md for the full table):

* **Observed:** slope `-0.0495` pressure-rate per second, SE `0.1005`,
  t `-0.49` on 30 degrees of freedom, 95% CI `[-0.2548, +0.1557]`, R² `0.015`.
  The interval contains zero comfortably, so the pass **passes**.
* **Shuffled null:** permuting `mean_time_to_throw_faced` across the 32 teams
  20,000 times fires the guard **4.81%** of the time, against the 5.00% an
  α=0.05 test fires by construction. The guard is calibrated, not dead and not
  trigger-happy — contrast `defense-vs-position`'s rank guard, whose null
  fired 54%.
* **Positive control:** injecting a genuine timing confound
  (`adj += k·(ttt − mean_ttt)`) leaves the guard passing at k=0.20 and fires
  it at k=0.30 and k=0.50. At df=30 the minimum detectable R² is **0.122**, so
  the guard catches a confound explaining ~12% or more of the cross-team
  variance in the adjusted rate — about 5.0 percentage points of pressure rate
  across the observed 0.236s spread in release timing, against a 15.0-point
  total spread. **It can fire.**

--------------------------------------------------------------------------
Why the adjustment does NOT residualise on this variable
--------------------------------------------------------------------------

The obvious way to "add the timing term" is to regress the adjusted rate on
team-mean time-to-throw and publish the residual. **That would make this guard
structurally incapable of firing**, because the residual of an OLS fit is
orthogonal to its own regressor by construction — the slope would be exactly
0.0 on every pass, for every dataset, including one where the confound is
total. An unfailable guard is worse than no guard: it reports a green number
forever.

So the timing term enters where it is real, and by a different route. The
opponent yardstick in `ratings.opponent_strengths` is the opposing offence's
own **pressure rate allowed**, computed leave-one-out over that offence's
*other* games — and an offence's own release timing is a significant driver of
what it allows. Measured at the offence level on the same 2025 season:
`pressure_allowed ~ own mean_time_to_throw` has slope `+0.106`/s, t `+2.10`,
r `+0.358`, over a `2.490–3.059`s spread. Offences that hold the ball longer
allow more pressure, so an offence with a quick release is rated as a strong
line, and a defence that faced it has its rate adjusted **up**.

That is the timing correction the spec asks for, carried by the opponent's own
*production* rather than bolted on as a second regressor — and because it is
estimated per opposing offence-game rather than from these 32 team means, the
residual slope below is a genuine test rather than an identity. The positive
control above is the proof: a real confound still moves the slope.

--------------------------------------------------------------------------
The distribution, and why there is no table of critical values here
--------------------------------------------------------------------------

`scipy` is not a fleet dependency and is not being added for one t-test.
Hard-coding `2.042` for df=30 would silently become wrong the first time a
week has a team with no charted dropbacks, so the Student-t two-sided p-value
is computed exactly, from `math.lgamma` and the regularised incomplete beta.
`tests/test_timing_guard.py` pins it against published critical values.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

__all__ = [
    "SIGNIFICANCE",
    "TimingRegression",
    "regularized_incomplete_beta",
    "residual_slope",
    "t_critical",
    "two_sided_p_value",
]

# **The spec's own standard: "statistically indistinguishable from zero".**
# The conventional 5%, chosen because it is the convention and NOT calibrated
# to what this season's data happens to produce — a threshold fitted to today's
# distribution stops being a guard and becomes a filter on tomorrow's signal.
# The shuffled null above confirms the realised rate matches the nominal one.
SIGNIFICANCE = 0.05

# Below four teams there is no residual degree of freedom worth the name, and
# the t-statistic's denominator is one or two points of noise. Fewer than this
# and the guard reports that it did not run rather than reporting a pass —
# see `residual_slope`.
MIN_TEAMS = 4

_TINY = 1e-300
_EPSILON = 3e-16
_MAX_ITERATIONS = 400


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz's continued fraction for the incomplete beta. Numerical Recipes.

    Converges for `x < (a+1)/(a+b+2)`; the caller reflects the other side.
    """
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITERATIONS + 1):
        m2 = 2 * m
        for numerator in (
            m * (b - m) * x / ((qam + m2) * (a + m2)),
            -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2)),
        ):
            d = 1.0 + numerator * d
            if abs(d) < _TINY:
                d = _TINY
            c = 1.0 + numerator / c
            if abs(c) < _TINY:
                c = _TINY
            d = 1.0 / d
            h *= d * c
        # The second half of each iteration is the convergence test; `d * c`
        # is the delta and it approaches 1.0.
        if abs(d * c - 1.0) < _EPSILON:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """`I_x(a, b)`, the regularised incomplete beta function."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def two_sided_p_value(t_statistic: float, degrees_of_freedom: int) -> float:
    """`P(|T| > |t|)` for Student's t on `degrees_of_freedom`.

    Exact rather than a normal approximation: at df=30 the 5% critical value
    is 2.042 and the normal's is 1.960, so a z-based interval would be 4%
    too narrow and would fire on a confound the t-test calls noise.
    """
    if degrees_of_freedom <= 0:
        return 1.0
    df = float(degrees_of_freedom)
    return regularized_incomplete_beta(
        df / 2.0, 0.5, df / (df + t_statistic * t_statistic)
    )


@lru_cache(maxsize=64)
def t_critical(degrees_of_freedom: int, *, alpha: float = SIGNIFICANCE) -> float:
    """The two-sided critical value, by bisection on `two_sided_p_value`.

    Inverted numerically rather than tabulated, so a short week with fewer
    comparable teams gets its own correct value instead of quietly reusing
    df=30's.

    Memoised because the bisection costs 200 evaluations of the incomplete
    beta and `(degrees_of_freedom, alpha)` is constant within a pass — and
    because it makes the shuffled-null test in `tests/test_timing_guard.py`
    affordable enough to actually run. Pure, so the cache cannot go stale; the
    key space is bounded by the number of teams.
    """
    low, high = 0.0, 1_000.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if two_sided_p_value(middle, degrees_of_freedom) > alpha:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


@dataclass(frozen=True)
class TimingRegression:
    """One pass's answer to the spec's assertion.

    `flagged` is True when the residual slope is **distinguishable** from
    zero — that is, when the guard has found the confound the spec describes.
    The rows are published either way, carrying `timing_confound_flagged`, for
    the same reason `defense-vs-position` publishes a flagged rank divergence:
    "for manual review" asks for a consumer that can discount the row and an
    operator who can go and look, not for a silently dropped week.
    """

    teams: int
    slope: float
    stderr: float
    t_statistic: float
    degrees_of_freedom: int
    p_value: float
    ci_low: float
    ci_high: float
    flagged: bool


def residual_slope(
    timing: Sequence[float],
    adjusted: Sequence[float],
    *,
    alpha: float = SIGNIFICANCE,
) -> TimingRegression | None:
    """Regress `adjusted` on `timing`; `None` when the guard cannot run.

    `None` is returned — never a passing result — when there are too few teams
    or when `timing` has no variance across them. Both are states in which the
    slope is undefined or meaningless, and returning a `flagged=False` for
    either would report a clean bill of health the data cannot support. The
    caller files it as an explicit coverage error, because "the guard did not
    run" and "the guard ran and found nothing" are different facts and only
    one of them is reassuring.

    `alpha` is a parameter so a test can drive both arms, **not** so a
    deployment can tune it. `SIGNIFICANCE` is the only value the collector
    passes.
    """
    if len(timing) != len(adjusted):
        raise ValueError(
            f"timing and adjusted must be the same length, "
            f"got {len(timing)} and {len(adjusted)}"
        )
    n = len(timing)
    if n < MIN_TEAMS:
        return None

    mean_x = statistics.fmean(timing)
    mean_y = statistics.fmean(adjusted)
    sxx = sum((value - mean_x) ** 2 for value in timing)
    if sxx <= 0.0:
        # Every team faced identical release timing. There is no gradient to
        # measure, so there is nothing to clear.
        return None

    sxy = sum(
        (a - mean_x) * (b - mean_y) for a, b in zip(timing, adjusted, strict=True)
    )
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residuals = [
        b - (intercept + slope * a) for a, b in zip(timing, adjusted, strict=True)
    ]
    degrees_of_freedom = n - 2
    residual_variance = sum(r * r for r in residuals) / degrees_of_freedom
    stderr = math.sqrt(residual_variance / sxx)

    if stderr <= 0.0:
        # A perfect fit: every point lies exactly on the line. That is not a
        # pass — it is either a degenerate fixture or an adjustment that IS a
        # function of the timing variable, which is exactly what this guard
        # exists to catch.
        t_statistic = math.inf if slope != 0.0 else 0.0
        p_value = 0.0 if slope != 0.0 else 1.0
        half_width = 0.0
    else:
        t_statistic = slope / stderr
        p_value = two_sided_p_value(t_statistic, degrees_of_freedom)
        half_width = t_critical(degrees_of_freedom, alpha=alpha) * stderr

    return TimingRegression(
        teams=n,
        slope=slope,
        stderr=stderr,
        t_statistic=t_statistic,
        degrees_of_freedom=degrees_of_freedom,
        p_value=p_value,
        ci_low=slope - half_width,
        ci_high=slope + half_width,
        # The interval and the p-value agree by construction; the p-value is
        # the test and the interval is what an operator reads.
        flagged=p_value < alpha,
    )

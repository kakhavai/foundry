"""Crew rates, and the two guards that decide whether one may be served.

The spec's named failure mode is the whole subject of this module: *"rates that
are pure sampling noise presented as tendencies. Seventeen games is enough for
`penalties_per_game` to be marginally informative and nowhere near enough for
`dpi_per_game`, where a crew's entire season may contain four calls — yet the
field is populated to three decimal places and reads as signal."*

Three mechanisms answer it, and none of them is advisory.

--------------------------------------------------------------------------
1. Shrinkage — a rate is an estimate, not a parameter
--------------------------------------------------------------------------

A raw sixteen-game mean is published as if the crew's true rate were known.
Every rate here is instead regressed toward the league mean by the standard
random-effects weight:

    sigma2  pooled WITHIN-crew variance of the per-game values
    tau2    BETWEEN-crew variance, net of sampling noise:
            max(0, Var(crew means) - sigma2 / mean_n)
    k       tau2 / (tau2 + sigma2 / n)          the crew's own weight
    value   mu + k * (raw - mu)

`k` is how much of the crew's apparent deviation survives once the noise that
could have produced it is removed. `tau2 = 0` — no evidence crews differ at all
— gives `k = 0` and publishes the league mean, which is the honest answer and
looks like a bug only if you expected the raw number.

`rate_stderr` is `sqrt(sigma2 / n)`: the standard error of the raw mean at
`games_sampled`, exactly as the spec defines it. It ships with every rate,
served or refused.

--------------------------------------------------------------------------
2. The split-half stability check, and the refusal
--------------------------------------------------------------------------

The spec: *"correlate each crew's odd-week and even-week rates within a season,
and refuse to serve any rate whose split-half correlation is not
distinguishable from zero without `is_shrunk = true`."*

So, per rate: take each crew's odd-week mean and even-week mean, correlate the
two across crews, and put a Fisher-z 95% interval on the result.
`stable = ci_low > 0`. At 17 crews that needs r > ~0.48.

Then the refusal, implemented rather than noted: **a rate that is not stable
and not shrunk is not served.** `value` is `null`, `served` is `false`, and
`refusal_reason` says which. A raw mean that correlates with nothing is a
number with no referent, and publishing it to three decimals is precisely the
failure mode.

Measured against the real 2025 regular season, **not one of the seven
per-game rates is stable** — the best is `penalty_yards_per_game` at r = 0.365,
CI low -0.140. Every one of them is also materially shrunk (k between 0.000 and
0.571), so every one is served, as a shrunk estimate, honestly labelled. That
is the collector working, not failing: the product is the crew's rate *plus*
the statement that at sixteen games it is barely distinguishable from average.

--------------------------------------------------------------------------
3. What this module does NOT decide
--------------------------------------------------------------------------

Crew churn. `crew_continuity_pct` is a property of an assignment, lives in
`officiating.crews`, and its alarm is raised in `capture.py` where both facts
are in scope — a low-continuity assignment only matters if that crew's rates
are actually being served. Two guards, deliberately not merged.
"""

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .adapters.pbp import REGULATION_SECONDS, GamePenalties

# The spec's `rate_window` enum. `season_to_date` is what this collector
# computes: every regular-season game of the captured season that has been
# played and has play-by-play. `trailing_17` and `career` are not built — they
# need multi-season play-by-play, which is a different cost decision.
RATE_WINDOW_SEASON_TO_DATE = "season_to_date"

# `k` at or above this counts as "not materially shrunk", so a rate here has
# effectively been published as its raw mean and `is_shrunk` must say false.
# It is what makes the spec's refusal reachable at all: without a threshold
# every rate is "shrunk" by some floating-point epsilon and nothing is ever
# refused.
SHRINKAGE_MATERIAL_BELOW = 0.99

# Fisher-z needs at least four crews before an interval means anything
# (`n - 3` in the denominator). Below that, stability is UNKNOWN — which this
# module treats as not-stable, so the rate must be shrunk to be served. The
# conservative direction is the correct one: an unknown correlation is not
# evidence of a real one.
MIN_CREWS_FOR_STABILITY = 4

# 95%, two-sided.
Z_95 = 1.959963985


def _seconds_per_play(game: GamePenalties) -> float:
    return REGULATION_SECONDS / game.offensive_plays


# name -> one game's contribution to that rate. The spec's field table, in its
# order. Every one is a per-GAME quantity averaged over the window, which is
# what makes a single `games_sampled` and a single split-half procedure apply
# uniformly to all of them.
RATE_EXTRACTORS: dict[str, callable] = {
    "penalties_per_game": lambda g: float(g.penalties),
    "penalty_yards_per_game": lambda g: float(g.penalty_yards),
    "dpi_per_game": lambda g: float(g.dpi),
    "dpi_yards_per_game": lambda g: float(g.dpi_yards),
    "offensive_holding_per_game": lambda g: float(g.offensive_holding),
    "defensive_holding_per_game": lambda g: float(g.defensive_holding),
    "plays_per_game": lambda g: float(g.offensive_plays),
    "seconds_per_play": _seconds_per_play,
}

RATE_NAMES: tuple[str, ...] = tuple(RATE_EXTRACTORS)

REASON_UNSTABLE_AND_UNSHRUNK = "unstable_and_unshrunk"
REASON_NO_SAMPLE = "no_games_sampled"


@dataclass(frozen=True)
class CrewSample:
    """One crew's window: the per-game observations every rate is drawn from.

    `weeks` is parallel to `games` and is what the split-half check partitions
    on. Carried alongside rather than derived, because "odd week" is a fact
    about the schedule and this module must not have to know how to find it.
    """

    crew_id: str
    weeks: tuple[int, ...]
    games: tuple[GamePenalties, ...]

    def values(self, rate: str) -> list[float]:
        return [RATE_EXTRACTORS[rate](game) for game in self.games]

    def half_values(self, rate: str, *, odd: bool) -> list[float]:
        return [
            RATE_EXTRACTORS[rate](game)
            for week, game in zip(self.weeks, self.games, strict=True)
            if (week % 2 == 1) is odd
        ]


@dataclass(frozen=True)
class RatePriors:
    """One rate's league-level parameters, estimated across every crew."""

    mu: float
    sigma2: float
    tau2: float
    split_half_r: float | None
    split_half_ci_low: float | None

    @property
    def stable(self) -> bool:
        """Distinguishable from zero — the spec's phrase, made a number.

        `None` (too few crews to interval) is deliberately not stable. See
        `MIN_CREWS_FOR_STABILITY`.
        """
        return self.split_half_ci_low is not None and self.split_half_ci_low > 0.0


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or `None` when either side has no variance.

    `None` rather than 0.0, because they mean different things: zero variance
    is "this sample cannot answer the question", and a caller that reads it as
    a measured zero would report a rate as *proven* noise when it is merely
    unexamined. Both end in the same place — not stable — but only one of them
    is honest about why.
    """
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    denominator = math.sqrt(
        sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys)
    )
    if denominator == 0.0:
        return None
    return numerator / denominator


def fisher_ci_low(r: float, n: int) -> float | None:
    """Lower bound of the 95% interval on `r`, via the Fisher z transform."""
    if n < MIN_CREWS_FOR_STABILITY:
        return None
    # atanh is undefined at +-1; a perfect correlation has a lower bound that
    # is certainly above zero, so clamping rather than refusing is right.
    r = max(min(r, 0.999999), -0.999999)
    z = math.atanh(r)
    return math.tanh(z - Z_95 / math.sqrt(n - 3))


def split_half(samples: Sequence[CrewSample], rate: str) -> tuple[float | None, ...]:
    """Correlate odd-week against even-week crew means, across crews.

    A crew with no games in one half contributes nothing — it cannot be a
    point on the scatter. Returns `(r, ci_low)`.
    """
    odd: list[float] = []
    even: list[float] = []
    for sample in samples:
        odd_values = sample.half_values(rate, odd=True)
        even_values = sample.half_values(rate, odd=False)
        if not odd_values or not even_values:
            continue
        odd.append(statistics.fmean(odd_values))
        even.append(statistics.fmean(even_values))

    r = pearson(odd, even)
    if r is None:
        return (None, None)
    return (r, fisher_ci_low(r, len(odd)))


def estimate_priors(samples: Sequence[CrewSample], rate: str) -> RatePriors:
    """League mean, within/between variance and stability for one rate.

    `sigma2` is pooled across crews rather than taken per crew: a single crew's
    sixteen games estimate their own variance badly, and the quantity wanted is
    how noisy *a game* is, which is a league-level fact.
    """
    per_crew = [sample.values(rate) for sample in samples if sample.games]
    flat = [value for values in per_crew for value in values]
    if not flat:
        return RatePriors(0.0, 0.0, 0.0, None, None)

    mu = statistics.fmean(flat)

    total_games = len(flat)
    crew_count = len(per_crew)
    residual_dof = total_games - crew_count
    if residual_dof > 0:
        within_sum = sum(
            sum((value - statistics.fmean(values)) ** 2 for value in values)
            for values in per_crew
            if len(values) > 1
        )
        sigma2 = within_sum / residual_dof
    else:
        # One game per crew: every observation IS its crew's mean, so there is
        # no within-crew signal to measure. Zero here propagates to k = 1 (no
        # shrinkage), which is exactly the case the refusal must catch rather
        # than something to paper over with a fabricated variance.
        sigma2 = 0.0

    means = [statistics.fmean(values) for values in per_crew]
    if crew_count > 1:
        mean_n = total_games / crew_count
        # The observed spread of crew means already contains sampling noise;
        # subtracting sigma2/n is what leaves the part attributable to the
        # crews themselves. Clamped at zero: a negative estimate means the
        # spread is smaller than noise alone would produce, i.e. no evidence of
        # any between-crew difference.
        tau2 = max(0.0, statistics.variance(means) - sigma2 / mean_n)
    else:
        tau2 = 0.0

    r, ci_low = split_half(samples, rate)
    return RatePriors(
        mu=mu,
        sigma2=sigma2,
        tau2=tau2,
        split_half_r=r,
        split_half_ci_low=ci_low,
    )


def build_rate(
    sample: CrewSample, rate: str, priors: RatePriors
) -> dict[str, float | bool | str | None]:
    """One crew's one rate, with everything needed to judge it.

    The spec lists `is_shrunk` and `rate_stderr` as scalar fields of the
    signal. They cannot be: there are eight rates with independent variances
    and independent stability, and one bool cannot describe eight of them. So
    each spec-named rate field carries an OBJECT with its own
    `is_shrunk`/`stderr`, and the deviation is disclosed rather than resolved
    by publishing one number that would be wrong for seven rates.
    """
    values = sample.values(rate)
    n = len(values)
    if n == 0:
        return {
            "value": None,
            "raw": None,
            "stderr": None,
            "games_sampled": 0,
            "is_shrunk": False,
            "shrinkage_factor": None,
            "split_half_r": priors.split_half_r,
            "split_half_ci_low": priors.split_half_ci_low,
            "stable": priors.stable,
            "served": False,
            "refusal_reason": REASON_NO_SAMPLE,
        }

    raw = statistics.fmean(values)
    stderr = math.sqrt(priors.sigma2 / n)

    denominator = priors.tau2 + priors.sigma2 / n
    # No variance anywhere — every crew identical, every game identical. The
    # crew's mean IS the league mean, so there is nothing to shrink and k = 1
    # is the truthful weight.
    k = 1.0 if denominator == 0.0 else priors.tau2 / denominator

    is_shrunk = k < SHRINKAGE_MATERIAL_BELOW
    value = priors.mu + k * (raw - priors.mu) if is_shrunk else raw

    # THE REFUSAL. A rate whose split-half correlation is not distinguishable
    # from zero may only be served with `is_shrunk = true`.
    served = priors.stable or is_shrunk

    return {
        "value": round(value, 4) if served else None,
        # `raw` survives the refusal on purpose: a consumer that wants to do
        # its own statistics is entitled to the observation. It is `value` —
        # the field a naive consumer reads — that goes null.
        "raw": round(raw, 4),
        "stderr": round(stderr, 4),
        "games_sampled": n,
        "is_shrunk": is_shrunk,
        "shrinkage_factor": round(k, 4),
        "split_half_r": None
        if priors.split_half_r is None
        else round(priors.split_half_r, 4),
        "split_half_ci_low": None
        if priors.split_half_ci_low is None
        else round(priors.split_half_ci_low, 4),
        "stable": priors.stable,
        "served": served,
        "refusal_reason": None if served else REASON_UNSTABLE_AND_UNSHRUNK,
    }


def build_all_priors(samples: Sequence[CrewSample]) -> Mapping[str, RatePriors]:
    """Every rate's league parameters, estimated once for the whole pass."""
    return {rate: estimate_priors(samples, rate) for rate in RATE_NAMES}

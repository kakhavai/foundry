"""The derived fields, computed here so the derivation is versioned with us.

The spec is explicit: `age_years`, `position_age_percentile`,
`draft_capital_score` and `career_snap_load_percentile` are "computed by the
collector from raw upstream values, never accepted from the upstream, so that
the derivation is versioned with the collector". None of the four is read off a
wire format; every one is a pure function of values this module is handed.

--------------------------------------------------------------------------
The reference population, stated because an undocumented one is unreproducible
--------------------------------------------------------------------------

Both percentiles rank a player against **the population this pass captured** —
the `roster-scope` watchlist players whose profiles resolved in *this* capture —
and not against all of history, and not against every player in `players.csv`.

That is a deliberate choice with a real cost, and both halves belong in the
open:

* It is the right reference for the question a projection model asks. "Is this
  running back old *for a starting running back*" is a question about the
  players currently occupying those roles, not about every back who ever
  registered a snap. A career-spanning denominator would push every active
  player toward the same percentile every season.
* It makes the number **unstable at small populations**. A cohort of four is
  four evenly spaced values regardless of what the ages actually are, and the
  percentile then encodes rank rather than distribution. `MIN_COHORT` is the
  guard: below it the percentile is `null` rather than a number that looks
  computed. A null percentile is a fact a consumer can handle; a percentile
  computed from three players is one it cannot detect.

`GET /signals/age-curve` publishes the age distribution each percentile was
taken against, so a consumer can reproduce the bucketing rather than trust it —
which is only meaningful because the population is the pass's own and is
therefore recoverable from the pass's own output.

--------------------------------------------------------------------------
`position_age_curve_stage` is a prior, not a measurement
--------------------------------------------------------------------------

`POSITION_AGE_CURVE` below is a committed table of position-specific age
boundaries. It is a **modelling prior** — nobody upstream publishes it — and it
is here rather than in a config map for exactly the reason the spec gives for
the derived fields: it is versioned with the collector, so a lake object a
season old can be replayed against the table that produced it. The route
publishes the table alongside the distribution for the same reason.

The stage is a position-specific age cut rather than a percentile cut, and the
spec says so ("position-specific bucketing, not a global age cut"). Deriving it
from the percentile instead would make it a *relative* measure: in a season
where every starting back happened to be 27, a quarter of them would be
bucketed `cliff` purely for being the oldest of a young cohort.
"""

import math
from datetime import date

__all__ = [
    "CURVE_STAGES",
    "DRAFT_PICKS_PER_CLASS",
    "MIN_COHORT",
    "POSITION_AGE_CURVE",
    "age_years",
    "athleticism_composite",
    "career_snap_load_percentile",
    "draft_capital_score",
    "position_age_curve_stage",
    "position_age_distribution",
    "position_age_percentile",
]

# Below this a percentile encodes rank rather than distribution. See the module
# docstring. Five rather than two because four evenly spaced values is exactly
# the shape that looks computed and is not.
MIN_COHORT = 5

# The modern seven-round draft plus compensatory selections. 262 was the 2024
# and 2025 count; the exact figure only sets the curve's tail, and a pick beyond
# it clamps to 0.0 rather than going negative.
DRAFT_PICKS_PER_CLASS = 262

CURVE_STAGES = ("pre_peak", "peak", "post_peak", "cliff")

# `(peak_from, post_peak_from, cliff_from)` in decimal years, per position.
# A modelling prior — see the module docstring. Read as half-open bands:
#   age <  peak_from        -> pre_peak
#   peak_from <= age < post_peak_from -> peak
#   post_peak_from <= age < cliff_from -> post_peak
#   age >= cliff_from      -> cliff
#
# The positions differ because the underlying attrition does: a back's
# production falls off years before a quarterback's, and a kicker's barely does
# at all. These are the boundaries this collector commits to, not measured
# constants, and changing one is a versioned change to published output.
POSITION_AGE_CURVE: dict[str, tuple[float, float, float]] = {
    "QB": (25.0, 33.0, 38.0),
    "RB": (22.5, 26.5, 29.5),
    "WR": (23.5, 29.0, 32.0),
    "TE": (24.5, 29.5, 33.0),
    "K": (24.0, 36.0, 41.0),
}

DAYS_PER_YEAR = 365.25


def age_years(birth_date: date | None, as_of: date) -> float | None:
    """Age in decimal years as of `as_of`, or `None` without a birth date.

    `None`, never 0.0 and never a guess. The spec allows a null birth date
    ("null only when no adapter can supply it"), and an age of zero would flow
    into `position_age_curve_stage` as the most `pre_peak` player in the league.

    A birth date in the future yields a negative age rather than a clamp: that
    is a genuine upstream error and the caller records it, where a clamp to 0.0
    would launder it into a plausible value.
    """
    if birth_date is None:
        return None
    return round((as_of - birth_date).days / DAYS_PER_YEAR, 3)


def draft_capital_score(overall_pick: int | None) -> float:
    """Normalized 0.0–1.0 pedigree from the overall selection number.

        score = 1 - log(pick) / log(DRAFT_PICKS_PER_CLASS + 1)

    Pick 1 scores exactly 1.0; the last pick of a class scores near 0.0;
    undrafted is 0.0, which the spec states outright.

    Logarithmic rather than linear because draft capital is: the gap between
    picks 1 and 10 is worth far more than the gap between 200 and 210, and a
    linear score would value the second at the same 0.038. A supplemental or
    historical pick beyond the modern class size clamps to 0.0 rather than
    going negative — a negative pedigree is not a thing, and it would invert
    against undrafted.
    """
    if overall_pick is None or overall_pick < 1:
        return 0.0
    score = 1.0 - math.log(overall_pick) / math.log(DRAFT_PICKS_PER_CLASS + 1)
    return round(max(0.0, min(1.0, score)), 6)


def position_age_curve_stage(position: str, age: float | None) -> str | None:
    """Which band of its position's age curve a player sits in.

    `None` for an unknown age or a position the table does not carry — never a
    default band. Bucketing a player whose age nobody could supply is exactly
    the "well-formed record with the wrong prior" the spec's failure mode
    describes.
    """
    if age is None:
        return None
    bands = POSITION_AGE_CURVE.get(position)
    if bands is None:
        return None
    peak_from, post_peak_from, cliff_from = bands
    if age < peak_from:
        return "pre_peak"
    if age < post_peak_from:
        return "peak"
    if age < cliff_from:
        return "post_peak"
    return "cliff"


def _midrank_percentile(population: list[float], value: float) -> float:
    """The empirical mid-rank percentile of `value` within `population`.

        (count(v < value) + 0.5 * count(v == value)) / n

    Mid-rank rather than `count(v <= value) / n` so that ties share a position
    instead of the last-listed one scoring highest, and so a single-valued
    population reads 0.5 rather than 1.0. The result is in (0, 1) exclusive,
    which is what makes 0.0 and 1.0 available to mean something else if this
    ever needs them.
    """
    below = sum(1 for other in population if other < value)
    equal = sum(1 for other in population if other == value)
    return round((below + 0.5 * equal) / len(population), 6)


def _cohort_percentile(
    cohorts: dict, key, value: float | None, *, min_cohort: int = MIN_COHORT
) -> float | None:
    if value is None:
        return None
    population = cohorts.get(key)
    if not population or len(population) < min_cohort:
        return None
    return _midrank_percentile(population, value)


def position_age_percentile(
    ages_by_position: dict[str, list[float]],
    position: str,
    age: float | None,
    *,
    min_cohort: int = MIN_COHORT,
) -> float | None:
    """Age relative to the position's captured age distribution, 0.0–1.0.

    `ages_by_position` is built from the pass's own population — see the module
    docstring for why that reference set is the right one and what it costs.
    `None` below `min_cohort`, rather than a number that looks computed.
    """
    return _cohort_percentile(ages_by_position, position, age, min_cohort=min_cohort)


def career_snap_load_percentile(
    snaps_by_cohort: dict[tuple[str, int], list[float]],
    position: str,
    experience_seasons: int | None,
    snaps: int | None,
    *,
    min_cohort: int = MIN_COHORT,
) -> float | None:
    """Career snaps relative to same-position players of equal experience.

    The cohort is `(position, experience_seasons)`, which is the spec's own
    definition and is also why this one hits `MIN_COHORT` far more often than
    the age percentile does: a 384-player scope splits into roughly 60 cohorts,
    and the veteran ones are small by construction. A null here is the honest
    answer — there is no comparable population — and it is preferable to a
    number computed from two other players.
    """
    if experience_seasons is None:
        return None
    return _cohort_percentile(
        snaps_by_cohort,
        (position, experience_seasons),
        None if snaps is None else float(snaps),
        min_cohort=min_cohort,
    )


def position_age_distribution(ages: list[float]) -> dict:
    """One position's captured age distribution, as `/signals/age-curve` serves it.

    Quantiles are read straight off the sorted sample by nearest rank rather
    than interpolated, so a consumer reproducing `position_age_percentile` from
    this block gets the same arithmetic the collector used rather than a
    smoothed approximation of it.
    """
    ordered = sorted(ages)
    count = len(ordered)
    if count == 0:
        return {"count": 0, "min": None, "max": None, "mean": None, "quantiles": {}}

    def _at(fraction: float) -> float:
        index = min(count - 1, max(0, math.ceil(fraction * count) - 1))
        return ordered[index]

    return {
        "count": count,
        "min": ordered[0],
        "max": ordered[-1],
        "mean": round(sum(ordered) / count, 3),
        "quantiles": {
            "p10": _at(0.10),
            "p25": _at(0.25),
            "p50": _at(0.50),
            "p75": _at(0.75),
            "p90": _at(0.90),
        },
    }


# The measurements that make up `athleticism.composite_score`, with the
# direction each one improves in. Lower is better for the timed drills, higher
# for the explosive ones.
COMPOSITE_COMPONENTS: dict[str, tuple[float, float, bool]] = {
    # field: (worst, best, lower_is_better)
    "forty_yard": (5.30, 4.20, True),
    "three_cone": (7.80, 6.50, True),
    "shuttle": (4.90, 3.90, True),
    "vertical_inches": (25.0, 45.0, False),
    "broad_jump_inches": (95.0, 145.0, False),
    "bench_reps": (5.0, 40.0, False),
}


def athleticism_composite(measurements: dict) -> float | None:
    """A 0.0–1.0 composite over whichever measurements are present.

    `None` — never 0.0 — when a player has no measurements at all. The spec is
    blunt about why: "an untested player has null combine fields, not zeros,
    because a zero forty time is numerically catastrophic downstream", and a
    composite of 0.0 says "measured, and terrible" rather than "never tested".

    Each component is min-max scaled against the fixed bounds in
    `COMPOSITE_COMPONENTS` and averaged over the ones present, so a player with
    three drills is comparable to one with six rather than being penalised for
    the absent three. The bounds are a committed prior, exactly like
    `POSITION_AGE_CURVE`, and out-of-range values clamp rather than escaping
    [0, 1].
    """
    scaled: list[float] = []
    for name, (worst, best, _lower_is_better) in COMPOSITE_COMPONENTS.items():
        value = measurements.get(name)
        if value is None:
            continue
        fraction = (float(value) - worst) / (best - worst)
        scaled.append(max(0.0, min(1.0, fraction)))
    if not scaled:
        return None
    return round(sum(scaled) / len(scaled), 6)

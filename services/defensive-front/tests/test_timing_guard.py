"""The timing-confound guard: the distribution, both arms, and its limits.

The spec's named failure mode and the one part of this collector a reviewer
should read first. Three things are proved here and each has a counterpart
that shipped broken somewhere in this fleet:

* **The distribution is right.** No hard-coded 2.042. `scipy` is not a fleet
  dependency, so `two_sided_p_value` is the regularised incomplete beta, and
  it is pinned against published critical values rather than against itself.
* **The guard fires.** `coaching-scheme`'s changepoint detector fired on 65%
  of teams against a 55% shuffled null and shipped disabled; the only reason
  anyone knew was that somebody ran it. Both arms are driven here.
* **The guard reports when it CANNOT run.** A guard that silently stops
  running looks exactly like one that keeps passing.
"""

import math
import statistics
from itertools import permutations

import pytest

from defensive_front.capture import (
    REASON_TIMING_CONFOUND,
    REASON_TIMING_GUARD_NOT_RUN,
    STRENGTH,
    reset_published_digests,
)
from defensive_front.timing import (
    SIGNIFICANCE,
    regularized_incomplete_beta,
    residual_slope,
    t_critical,
    two_sided_p_value,
)

from . import season as season_module
from .conftest import Feeds, SpyLake, by_team, run_capture

# --------------------------------------------------------------------------
# The distribution
# --------------------------------------------------------------------------

# Published two-sided 5% Student-t critical values. Independent of this
# implementation, which is the entire point: a t-test verified against its own
# output verifies nothing.
PUBLISHED_CRITICAL_VALUES = {
    1: 12.706,
    2: 4.303,
    5: 2.571,
    10: 2.228,
    20: 2.086,
    30: 2.042,
    60: 2.000,
    120: 1.980,
}


@pytest.mark.parametrize(("df", "expected"), sorted(PUBLISHED_CRITICAL_VALUES.items()))
def test_the_critical_value_matches_published_tables(df, expected):
    assert t_critical(df) == pytest.approx(expected, abs=0.001)


def test_thirty_degrees_of_freedom_is_not_the_normal_approximation():
    """A z-based interval at df=30 is 4% too narrow, which is the difference
    between a confound the test calls noise and one it flags."""
    assert t_critical(30) > 1.960
    assert t_critical(30) == pytest.approx(2.042, abs=0.001)
    # ...and it converges on the normal as df grows, so it is the right
    # distribution rather than a constant that happens to be near 2.
    assert t_critical(100_000) == pytest.approx(1.960, abs=0.002)


def test_the_p_value_is_a_valid_survival_function():
    assert two_sided_p_value(0.0, 30) == pytest.approx(1.0)
    # Monotone decreasing in |t|.
    values = [two_sided_p_value(t, 30) for t in (0.5, 1.0, 2.0, 4.0, 8.0)]
    assert values == sorted(values, reverse=True)
    assert 0.0 <= values[-1] < 0.001


def test_the_p_value_and_the_critical_value_agree():
    """They are two readings of one test; if they disagreed, a row could be
    flagged while its published interval contained zero."""
    for df in (4, 10, 30, 60):
        critical = t_critical(df)
        assert two_sided_p_value(critical, df) == pytest.approx(SIGNIFICANCE, abs=1e-6)


def test_zero_degrees_of_freedom_is_never_significant():
    assert two_sided_p_value(99.0, 0) == 1.0


@pytest.mark.parametrize(
    ("a", "b", "x", "expected"),
    [
        (2.0, 3.0, 0.0, 0.0),
        (2.0, 3.0, 1.0, 1.0),
        # I_x(1,1) == x, the uniform case, which has a closed form.
        (1.0, 1.0, 0.25, 0.25),
        (1.0, 1.0, 0.75, 0.75),
        # I_x(a,b) == 1 - I_{1-x}(b,a), the reflection identity that the two
        # branches of the continued fraction have to agree across.
        (3.0, 7.0, 0.2, 1.0 - regularized_incomplete_beta(7.0, 3.0, 0.8)),
    ],
)
def test_the_incomplete_beta_has_its_known_values(a, b, x, expected):
    assert regularized_incomplete_beta(a, b, x) == pytest.approx(expected, abs=1e-9)


# --------------------------------------------------------------------------
# The guard: both arms
# --------------------------------------------------------------------------

# A confound-free pair: adjusted pressure rate unrelated to release timing.
CLEAN_TIMING = [2.60, 2.65, 2.70, 2.72, 2.75, 2.78, 2.80, 2.84]
CLEAN_ADJUSTED = [0.28, 0.34, 0.26, 0.36, 0.29, 0.33, 0.27, 0.35]


def test_a_confound_free_league_passes():
    result = residual_slope(CLEAN_TIMING, CLEAN_ADJUSTED)
    assert result is not None
    assert not result.flagged
    assert result.ci_low < 0.0 < result.ci_high, (
        "an interval that excludes zero has flagged the pass"
    )
    assert result.p_value > SIGNIFICANCE
    assert result.degrees_of_freedom == len(CLEAN_TIMING) - 2


@pytest.mark.parametrize("k", [0.5, 1.0, 2.0])
def test_an_injected_confound_fires_the_guard(k):
    """The positive control. A guard that cannot be made to fire is a
    decoration that reports green forever."""
    mean = statistics.fmean(CLEAN_TIMING)
    confounded = [
        base + k * (timing - mean)
        for base, timing in zip(CLEAN_ADJUSTED, CLEAN_TIMING, strict=True)
    ]
    result = residual_slope(CLEAN_TIMING, confounded)
    assert result is not None
    assert result.flagged, f"k={k} left the guard silent"
    assert result.slope > 0.0
    assert result.ci_low > 0.0, "a flagged pass whose interval contains zero"


def test_the_guard_detects_a_confound_in_either_direction():
    """A front that draws quick-game opponents posts a DEPRESSED rate, so the
    spec's failure mode has a negative slope. A one-sided test would miss it
    entirely."""
    mean = statistics.fmean(CLEAN_TIMING)
    confounded = [
        base - 1.0 * (timing - mean)
        for base, timing in zip(CLEAN_ADJUSTED, CLEAN_TIMING, strict=True)
    ]
    result = residual_slope(CLEAN_TIMING, confounded)
    assert result is not None and result.flagged
    assert result.slope < 0.0


def test_the_false_positive_rate_matches_the_nominal_one():
    """A shuffled null. `defense-vs-position`'s rank guard fires 16% of the
    time against a null that fires 54% — knowing that ratio is what made it
    trustworthy. This guard is an exact test, so its realised rate must sit on
    its nominal 5%, and a drift away from it means the arithmetic is wrong.

    Deterministic permutations rather than a seeded RNG, so the number this
    asserts cannot move with a Python release. Every 37th permutation of the
    40,320 — a stride coprime with 8! so the sample is not a coset of any
    positional subgroup, which a stride of 40 would be.
    """
    timing = [2.55 + 0.04 * index for index in range(8)]
    adjusted = [0.24, 0.36, 0.28, 0.33, 0.26, 0.38, 0.30, 0.25]

    fired = 0
    total = 0
    for index, permuted in enumerate(permutations(adjusted)):
        if index % 37:
            continue
        total += 1
        result = residual_slope(timing, list(permuted))
        assert result is not None
        fired += result.flagged
    rate = fired / total
    assert 0.02 <= rate <= 0.09, (
        f"a correctly sized alpha=0.05 test fires ~5% of the time on a null; "
        f"this one fired {rate:.2%} over {total} permutations"
    )


# --------------------------------------------------------------------------
# When the guard cannot run
# --------------------------------------------------------------------------


def test_too_few_teams_reports_not_run_rather_than_a_pass():
    """`None`, never a `flagged=False`. "The guard did not run" and "the guard
    ran and found nothing" are different facts and only one is reassuring."""
    assert residual_slope([2.6, 2.7, 2.8], [0.30, 0.31, 0.29]) is None


def test_identical_release_timing_reports_not_run():
    """Every defence facing the same release timing leaves no gradient to
    measure. Reporting a clean pass there would be a claim the data cannot
    support — and it is exactly what a balanced round-robin schedule produces,
    so it is reachable rather than theoretical."""
    assert residual_slope([2.7] * 8, CLEAN_ADJUSTED) is None


def test_mismatched_lengths_raise_rather_than_zip_short():
    """A silent `zip` would regress the first n teams against the first n
    timings and report a confident answer about a truncated league."""
    with pytest.raises(ValueError):
        residual_slope(CLEAN_TIMING, CLEAN_ADJUSTED[:-1])


def test_a_perfect_fit_is_flagged_rather_than_passed():
    """Zero residual variance is not a clean bill of health: it means the
    adjusted column IS a function of the timing variable, which is the
    strongest possible form of the confound. Reachable if anyone ever
    residualises the adjustment on this regressor — the change this guard
    exists to make impossible."""
    exact = [0.10 + 0.5 * timing for timing in CLEAN_TIMING]
    result = residual_slope(CLEAN_TIMING, exact)
    assert result is not None
    assert result.flagged
    assert math.isinf(result.t_statistic)


def test_a_perfectly_flat_column_is_not_flagged():
    """The other zero-variance case, and the opposite verdict: a constant
    adjusted column has slope exactly zero, which is a pass on this test even
    though it is a catastrophe on the variance one. The two guards answer
    different questions and neither substitutes for the other."""
    result = residual_slope(CLEAN_TIMING, [0.3] * len(CLEAN_TIMING))
    assert result is not None
    assert not result.flagged
    assert result.slope == 0.0


# --------------------------------------------------------------------------
# End to end: the two claims about the adjustment, on the real capture path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("k", [0.0, 0.1, 0.3, 0.6])
async def test_an_offense_mediated_timing_effect_is_ABSORBED(k):
    """**The evidence for this collector's central claim.**

    The spec says a non-zero residual slope "means the adjustment model is
    missing the timing term entirely". It is not missing: the timing term
    arrives through the opponent yardstick, because an offence's own release
    timing drives the pressure it concedes and that concession IS the
    leave-one-out yardstick. Measured on the real 2025 season at the offence
    level: `pressure_allowed ~ own mean_time_to_throw`, slope `+0.106`/s,
    t `+2.10`, r `+0.358` over a `2.490-3.059`s spread.

    So a confound injected through the offence must be removed however hard it
    is driven — and it is, up to six times the effect the real data shows.
    """
    reset_published_digests()
    envelopes = await run_capture(Feeds(timing_confound=k), lake=SpyLake())
    rows = by_team(envelopes)
    assert not next(iter(rows.values()))["timing_confound_flagged"], (
        f"the adjustment failed to absorb an offense-mediated confound at k={k}"
    )
    assert REASON_TIMING_CONFOUND not in {
        error["reason"] for error in envelopes[STRENGTH].errors
    }


@pytest.mark.parametrize("shift", [1.0, 2.0, 4.0])
async def test_a_confound_the_adjustment_cannot_absorb_FIRES_the_guard(shift):
    """The positive control, end to end.

    Release timing here tracks the DEFENCE's own strength without moving any
    offence's aggregate production, so there is nothing in the yardstick to
    absorb it with. The residual slope moves and the guard must fire —
    otherwise the passing result above is a guard that cannot fire rather than
    an adjustment that works.
    """
    reset_published_digests()
    envelopes = await run_capture(Feeds(defense_release_shift=shift), lake=SpyLake())
    assert all(row["timing_confound_flagged"] for row in by_team(envelopes).values())
    errors = {error["reason"]: error["detail"] for error in envelopes[STRENGTH].errors}
    assert REASON_TIMING_CONFOUND in errors
    # The detail carries the numbers an operator needs to go and look, which
    # is what "flag for manual review" asks for.
    detail = errors[REASON_TIMING_CONFOUND]
    for fragment in ("slope=", "95% CI", "t=", "df", "p="):
        assert fragment in detail, detail


async def test_a_clean_pass_flags_nothing_and_files_no_guard_error():
    """The negative arm. Without it, a collector that flagged every pass would
    pass the positive control above."""
    envelopes = await run_capture(Feeds(), lake=SpyLake())
    assert not any(
        row["timing_confound_flagged"] for row in by_team(envelopes).values()
    )
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert REASON_TIMING_CONFOUND not in reasons
    assert REASON_TIMING_GUARD_NOT_RUN not in reasons, (
        "the guard did not run at all, so the clean verdict means nothing"
    )


async def test_a_charting_outage_reports_the_guard_as_NOT_RUN():
    """With no charted release times the regressor is empty and the guard
    cannot run. It must say so rather than report a clean pass: a guard that
    silently stops running looks exactly like one that keeps passing."""
    built = season_module.build_season()
    stripped = [
        season_module.Play(**{**vars(play), "time_to_throw": None})
        for play in built.plays
    ]
    feeds = Feeds(
        bodies={
            "participation": season_module.participation_document(
                season_module.Season(plays=stripped, season=built.season)
            )
        }
    )
    envelopes = await run_capture(feeds, lake=SpyLake())
    reasons = {error["reason"] for error in envelopes[STRENGTH].errors}
    assert REASON_TIMING_GUARD_NOT_RUN in reasons, reasons
    assert not any(
        row["timing_confound_flagged"] for row in by_team(envelopes).values()
    )
    for row in by_team(envelopes).values():
        assert row["mean_time_to_throw_faced"] is None


def test_the_interval_uses_the_t_critical_value_not_1_point_96():
    """**The CI half-width is `t_critical(df) * stderr`, and nothing else.**

    A mutant replacing it with a hard-coded `1.96` survives every test that
    only asks whether the interval contains zero, because the VERDICT comes
    from the p-value and the interval is merely reported. But the interval is
    what an operator reads, and at small df the normal is badly too narrow —
    2.4469 against 1.96 at df=6, a 25% understatement.
    """
    result = residual_slope(CLEAN_TIMING, CLEAN_ADJUSTED)
    assert result is not None
    half_width = result.ci_high - result.slope
    assert half_width == pytest.approx(
        t_critical(result.degrees_of_freedom) * result.stderr
    )
    assert half_width != pytest.approx(1.96 * result.stderr)
    assert result.slope - result.ci_low == pytest.approx(half_width)


def test_an_interval_that_the_normal_would_wrongly_exclude_zero_from():
    """The same mutant, caught by consequence rather than by construction.

    This fixture's t sits between 1.96 and the true critical value, so a
    normal-approximation interval **excludes** zero while the correct t
    interval contains it — a reported interval that contradicts its own
    verdict. Six teams, because that is where the two disagree most.
    """
    timing = [2.60, 2.64, 2.68, 2.72, 2.76, 2.80]
    adjusted = [0.265, 0.238, 0.249, 0.264, 0.317, 0.303]
    result = residual_slope(timing, adjusted)
    assert result is not None
    assert 1.96 < abs(result.t_statistic) < t_critical(result.degrees_of_freedom), (
        f"the fixture no longer sits between the two critical values: "
        f"t={result.t_statistic:.4f}, t_crit="
        f"{t_critical(result.degrees_of_freedom):.4f}"
    )
    assert not result.flagged
    assert result.ci_low < 0.0 < result.ci_high, (
        "the published interval excludes zero on a pass the test did not flag"
    )
    # ...which is exactly what the normal approximation would have produced.
    assert result.slope - 1.96 * result.stderr > 0.0


async def test_the_guard_regresses_the_ADJUSTED_column_not_the_raw_one():
    """**The whole point of the assertion, and it needs a fixture where the
    two disagree.**

    An offence-mediated confound at this strength leaves the RAW pressure rate
    tracking release timing hard enough to flag (t = +2.74 against a critical
    2.45 at df=6) while the adjusted column, which the yardstick has corrected,
    sits at t = +0.33. A guard pointed at the raw column would fire; the one
    the spec asks for must not.

    This is also the strongest available evidence that the adjustment is not
    "missing the timing term entirely": the timing signal is demonstrably
    there in the input and demonstrably gone from the output.
    """
    reset_published_digests()
    envelopes = await run_capture(Feeds(timing_confound=1.0), lake=SpyLake())
    rows = by_team(envelopes)

    timing = [row["mean_time_to_throw_faced"] for row in rows.values()]
    raw = residual_slope(timing, [r["pressure_rate_generated"] for r in rows.values()])
    adjusted = residual_slope(
        timing, [r["pressure_rate_generated_adj"] for r in rows.values()]
    )
    assert raw is not None and adjusted is not None
    assert raw.flagged, (
        "the fixture no longer carries a confound in the raw column, so it "
        f"cannot distinguish the two: t={raw.t_statistic:.3f}"
    )
    assert not adjusted.flagged, (
        f"the adjustment failed to remove it: t={adjusted.t_statistic:.3f}"
    )
    # ...and the PUBLISHED verdict follows the adjusted column.
    assert not any(row["timing_confound_flagged"] for row in rows.values())
    assert REASON_TIMING_CONFOUND not in {
        error["reason"] for error in envelopes[STRENGTH].errors
    }


def test_the_incomplete_beta_reflects_rather_than_extending_the_fraction():
    """The reflection branch, pinned by a value only it gets right.

    Lentz's continued fraction converges for `x < (a+1)/(a+b+2)` and the
    caller reflects the other side. Dropping the reflection is **not**
    equivalent: it is verdict-preserving at and around every critical value,
    but it errs by up to 0.096 in the p-value where `x -> 1`, worst at df=30
    and a t near zero, because the fraction has not converged in 400
    iterations there. `p_value` reaches the coverage-error detail an operator
    reads, so a silently wrong 0.90 for a 0.99 is worth one assertion.
    """
    assert two_sided_p_value(0.01, 30) == pytest.approx(0.99209, abs=1e-4)
    assert two_sided_p_value(0.10, 30) == pytest.approx(0.92101, abs=1e-4)
    # ...and the complement half of the same branch, whose own neighbour dies.
    assert regularized_incomplete_beta(0.5, 15.0, 0.9) == pytest.approx(1.0, abs=1e-9)


async def test_a_row_separates_a_clean_guard_from_one_that_could_not_run():
    """`timing_confound_flagged: false` means two very different things and a
    generator joins on the row, not on the coverage errors."""
    clean = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for row in clean.values():
        assert row["timing_guard_ran"] is True
        assert row["timing_confound_flagged"] is False

    reset_published_digests()
    built = season_module.build_season()
    stripped = [
        season_module.Play(**{**vars(play), "time_to_throw": None})
        for play in built.plays
    ]
    feeds = Feeds(
        bodies={
            "participation": season_module.participation_document(
                season_module.Season(plays=stripped, season=built.season)
            )
        }
    )
    unrun = by_team(await run_capture(feeds, lake=SpyLake()))
    for row in unrun.values():
        assert row["timing_guard_ran"] is False, (
            "a guard that could not run is reporting itself as one that passed"
        )
        assert row["timing_confound_flagged"] is False

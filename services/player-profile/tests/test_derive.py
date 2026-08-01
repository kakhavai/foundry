"""The four derived fields, which are the collector's own arithmetic.

The spec makes these the collector's responsibility precisely so the derivation
is versioned with it, which means nothing here is a pass-through and every value
is testable in isolation.

The recurring theme is **never a zero where the honest answer is null**. A zero
forty time, a zero age and a zero draft pick are all numerically catastrophic
downstream and all look like ordinary data.
"""

from datetime import date

import pytest

from player_profile.derive import (
    CURVE_STAGES,
    DRAFT_PICKS_PER_CLASS,
    MIN_COHORT,
    POSITION_AGE_CURVE,
    age_years,
    athleticism_composite,
    career_snap_load_percentile,
    draft_capital_score,
    position_age_curve_stage,
    position_age_distribution,
    position_age_percentile,
)

AS_OF = date(2026, 9, 15)


# ── age ──────────────────────────────────────────────────────────────────────


def test_age_is_decimal_years_as_of_the_given_date():
    assert age_years(date(2000, 9, 15), AS_OF) == pytest.approx(26.0, abs=0.02)


def test_a_missing_birth_date_is_null_not_zero():
    """A zero age would be the most `pre_peak` player in the league."""
    assert age_years(None, AS_OF) is None


def test_age_moves_with_the_as_of_date():
    """`age_years` is defined as of `scope.as_of_date`, so it MUST move. This is
    also why the digest gate republishes biographical rows daily."""
    born = date(2000, 1, 1)
    assert age_years(born, date(2026, 1, 1)) < age_years(born, date(2027, 1, 1))


# ── draft capital ────────────────────────────────────────────────────────────


def test_the_first_overall_pick_scores_exactly_one():
    assert draft_capital_score(1) == 1.0


def test_undrafted_is_zero_which_the_spec_states_outright():
    assert draft_capital_score(None) == 0.0


def test_the_score_decreases_monotonically_with_the_pick():
    picks = [1, 5, 32, 64, 143, 240, DRAFT_PICKS_PER_CLASS]
    scores = [draft_capital_score(pick) for pick in picks]
    assert scores == sorted(scores, reverse=True)
    assert len(scores) == len(picks)


def test_the_score_is_logarithmic_not_linear():
    """The gap between picks 1 and 10 is worth far more than the gap between 200
    and 210. A linear score would value them identically, which is why this
    asserts on the SHAPE rather than on any single value."""
    early = draft_capital_score(1) - draft_capital_score(10)
    late = draft_capital_score(200) - draft_capital_score(210)
    assert early > late * 5


def test_a_pick_beyond_the_class_clamps_rather_than_going_negative():
    """A negative pedigree would sort BELOW undrafted, inverting the scale."""
    assert draft_capital_score(10_000) == 0.0
    assert draft_capital_score(0) == 0.0


def test_every_score_is_inside_the_declared_range():
    scores = [draft_capital_score(pick) for pick in range(1, 300)]
    assert len(scores) == 299
    assert all(0.0 <= score <= 1.0 for score in scores)


# ── the age curve ────────────────────────────────────────────────────────────


def test_the_curve_is_position_specific_not_a_global_age_cut():
    """The spec says so explicitly. A 27-year-old back and a 27-year-old
    quarterback are not at the same point on their curves, and a table where
    every position shared boundaries would silently make them so."""
    assert position_age_curve_stage("RB", 27.0) == "post_peak"
    assert position_age_curve_stage("QB", 27.0) == "peak"


def test_every_stage_is_reachable_for_a_running_back():
    stages = [position_age_curve_stage("RB", age) for age in (21.0, 24.0, 27.5, 31.0)]
    assert stages == ["pre_peak", "peak", "post_peak", "cliff"]
    assert set(stages) == set(CURVE_STAGES)


def test_an_unknown_age_gets_no_stage_rather_than_a_default_band():
    """Bucketing a player whose age nobody could supply is exactly the
    "well-formed record with the wrong prior" the spec's failure mode is."""
    assert position_age_curve_stage("RB", None) is None


def test_an_uncarried_position_gets_no_stage():
    assert position_age_curve_stage("DT", 27.0) is None


def test_every_carried_position_has_ordered_boundaries():
    for position, bands in POSITION_AGE_CURVE.items():
        assert list(bands) == sorted(bands), position
        assert len(bands) == 3, position


# ── percentiles ──────────────────────────────────────────────────────────────


def test_a_percentile_ranks_within_its_own_position():
    ages = {"RB": [22.0, 24.0, 26.0, 28.0, 30.0]}

    assert position_age_percentile(ages, "RB", 22.0) < 0.5
    assert position_age_percentile(ages, "RB", 30.0) > 0.5


def test_ties_share_a_position_rather_than_the_last_one_winning():
    """Mid-rank, so five identical ages all read 0.5 instead of the last-listed
    one reading 1.0."""
    ages = {"RB": [25.0] * 5}

    assert position_age_percentile(ages, "RB", 25.0) == 0.5


def test_a_cohort_below_the_minimum_yields_null_not_a_computed_looking_number():
    """Four evenly spaced values are four evenly spaced values whatever the ages
    actually are; the percentile then encodes rank rather than distribution.

    A null is a fact a consumer can handle. A percentile computed from three
    players is one it cannot detect.

    **`min_cohort` is passed explicitly rather than read from the constant.**
    Sizing the fixture as `MIN_COHORT - 1` made the test and the code two
    expressions of one number: lowering the constant lowered the fixture with
    it and the test stayed green through a mutation that switched the guard off
    entirely. `test_the_declared_minimum_cohort` below is what pins the value.
    """
    small = {"RB": [24.0] * 4}
    big = {"RB": [24.0] * 5}

    assert position_age_percentile(small, "RB", 24.0, min_cohort=5) is None
    assert position_age_percentile(big, "RB", 24.0, min_cohort=5) is not None


def test_the_declared_minimum_cohort():
    """The constant, pinned once and separately from the behaviour above.

    Changing it is a change to published output — every percentile that flips
    between a number and null — so it should take a deliberate test edit.
    """
    assert MIN_COHORT == 5


def test_a_percentile_for_an_unknown_position_is_null():
    assert position_age_percentile({"RB": [24.0] * 9}, "WR", 24.0) is None


def test_the_snap_load_cohort_is_position_and_experience_together():
    """The spec: "relative to same-position players of equal
    `experience_seasons`". A cohort keyed on position alone would compare a
    rookie against a ten-year veteran."""
    cohorts = {("RB", 3): [100.0, 200.0, 300.0, 400.0, 500.0]}

    assert career_snap_load_percentile(cohorts, "RB", 3, 500) > 0.5
    assert career_snap_load_percentile(cohorts, "RB", 3, 100) < 0.5
    # Same position, different experience — no cohort, so no number.
    assert career_snap_load_percentile(cohorts, "RB", 4, 500) is None


def test_snap_load_without_an_experience_count_is_null():
    cohorts = {("RB", 3): [100.0] * 9}
    assert career_snap_load_percentile(cohorts, "RB", None, 500) is None


def test_snap_load_without_a_snap_total_is_null_not_zero():
    """A player with no `pfr_id` has no join key, which is not the same fact as
    taking zero snaps — and zero would read as genuine bottom-of-cohort."""
    cohorts = {("RB", 3): [100.0] * 9}
    assert career_snap_load_percentile(cohorts, "RB", 3, None) is None


# ── the distribution the extra route publishes ───────────────────────────────


def test_the_distribution_reports_the_sample_the_percentile_used():
    ages = [22.0, 24.0, 26.0, 28.0, 30.0]

    block = position_age_distribution(ages)

    assert block["count"] == 5
    assert block["min"] == 22.0
    assert block["max"] == 30.0
    assert block["mean"] == 26.0
    assert block["quantiles"]["p50"] == 26.0


def test_an_empty_distribution_is_explicit_rather_than_zeros():
    block = position_age_distribution([])

    assert block["count"] == 0
    assert block["min"] is None
    assert block["mean"] is None


# ── athleticism composite ────────────────────────────────────────────────────


def test_an_untested_player_has_a_null_composite_not_zero():
    """A composite of 0.0 says "measured, and terrible". The spec's whole point
    is that "never tested" must not be expressible as a number."""
    empty = dict.fromkeys(
        [
            "forty_yard",
            "vertical_inches",
            "broad_jump_inches",
            "three_cone",
            "shuttle",
            "bench_reps",
        ]
    )

    assert athleticism_composite(empty) is None


def test_a_faster_forty_scores_higher_than_a_slower_one():
    """Lower is better for the timed drills. Getting the direction wrong on one
    component is silent — the composite stays in range and stays plausible."""
    fast = athleticism_composite({"forty_yard": 4.30})
    slow = athleticism_composite({"forty_yard": 5.10})

    assert fast > slow


def test_a_higher_vertical_scores_higher_than_a_lower_one():
    assert athleticism_composite({"vertical_inches": 42.0}) > athleticism_composite(
        {"vertical_inches": 28.0}
    )


def test_a_partial_test_battery_is_averaged_over_what_is_present():
    """A player with three drills is comparable to one with six rather than
    being penalised for the absent three."""
    both = athleticism_composite({"forty_yard": 4.40, "vertical_inches": 40.0})
    one = athleticism_composite({"forty_yard": 4.40})

    assert both is not None and one is not None
    assert 0.0 <= both <= 1.0


def test_an_out_of_range_measurement_clamps_rather_than_escaping_the_range():
    assert athleticism_composite({"forty_yard": 2.0}) == 1.0
    assert athleticism_composite({"forty_yard": 9.0}) == 0.0

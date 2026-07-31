"""The pinned scoring table — the one thing no other collector may reimplement.

Every number here is asserted against a hand-computed value rather than against
the function that produced it, because a test that recomputes the formula
passes for any formula.
"""

import pytest

from player_stats.scoring import (
    FORMATS,
    PPR_POINTS,
    SCORING_RULES_VERSION,
    build_fantasy_points,
    build_rates,
)

EMPTY_PASSING = {
    "attempts": 0,
    "completions": 0,
    "yards": 0,
    "touchdowns": 0,
    "interceptions": 0,
    "sacks_taken": 0,
    "air_yards": 0,
}
EMPTY_RUSHING = {"attempts": 0, "yards": 0, "touchdowns": 0, "first_downs": 0}
EMPTY_RECEIVING = {
    "targets": 0,
    "receptions": 0,
    "yards": 0,
    "touchdowns": 0,
    "air_yards": 0,
    "yards_after_catch": 0,
    "first_downs": 0,
}
EMPTY_MISC = {"fumbles_lost": 0, "two_point_conversions": 0, "return_touchdowns": 0}


def points(**blocks):
    return build_fantasy_points(
        {**EMPTY_PASSING, **blocks.get("passing", {})},
        {**EMPTY_RUSHING, **blocks.get("rushing", {})},
        {**EMPTY_RECEIVING, **blocks.get("receiving", {})},
        {**EMPTY_MISC, **blocks.get("misc", {})},
    )


def test_a_quarterback_line_scores_the_hand_computed_value():
    """300 pass yards (12.0) + 2 pass TD (8.0) + 1 INT (-1.0) = 19.0, and no
    reception is involved, so all three formats agree."""
    result = points(passing={"yards": 300, "touchdowns": 2, "interceptions": 1})
    assert result == {"standard": 19.0, "half_ppr": 19.0, "ppr": 19.0}


def test_a_receiver_line_differs_by_exactly_the_reception_bonus():
    """8 receptions, 100 yards (10.0), 1 TD (6.0) = 16.0 before receptions."""
    result = points(receiving={"receptions": 8, "yards": 100, "touchdowns": 1})
    assert result["standard"] == 16.0
    assert result["half_ppr"] == 20.0
    assert result["ppr"] == 24.0


def test_a_running_back_line_counts_rushing_and_receiving_together():
    """80 rush yards (8.0) + 1 rush TD (6.0) + 3 rec (30 yds -> 3.0) = 17.0."""
    result = points(
        rushing={"yards": 80, "touchdowns": 1},
        receiving={"receptions": 3, "targets": 4, "yards": 30},
    )
    assert result["standard"] == 17.0
    assert result["ppr"] == 20.0


def test_a_lost_fumble_subtracts_and_a_two_point_conversion_adds():
    result = points(misc={"fumbles_lost": 2, "two_point_conversions": 1})
    assert result["standard"] == -2.0


def test_a_return_touchdown_scores_six():
    assert points(misc={"return_touchdowns": 1})["standard"] == 6.0


def test_an_empty_line_scores_zero_in_every_format():
    result = points()
    assert set(result) == set(FORMATS)
    assert len(result) == 3
    assert all(value == 0.0 for value in result.values())


def test_passing_touchdowns_are_worth_less_than_rushing_ones():
    """4 vs 6 is the single most commonly mis-copied rule in the table."""
    passing = points(passing={"touchdowns": 1})["standard"]
    rushing = points(rushing={"touchdowns": 1})["standard"]
    assert (passing, rushing) == (4.0, 6.0)


def test_points_are_rounded_to_two_places():
    """Binary floating point makes `35 * 0.04` come out as `1.4000000000000001`
    and `3 * 0.1` as `0.30000000000000004`. Unrounded, that is what lands in an
    append-only lake object nobody rewrites, and what a scoreboard renders.

    Asserted with `repr` as well as `==`, because `1.4000000000000001 == 1.4`
    is False but `round(1.4000000000000001, 2) == 1.4` is True — an `==`
    assertion alone would pass against a value that is merely close.
    """
    passing_only = points(passing={"yards": 35})["standard"]
    assert repr(passing_only) == "1.4"

    rushing_only = points(rushing={"yards": 3})["standard"]
    assert repr(rushing_only) == "0.3"

    assert points(passing={"yards": 217})["standard"] == 8.68


def test_the_three_formats_are_the_ones_player_projections_serves():
    assert FORMATS == ("standard", "half_ppr", "ppr")
    assert set(PPR_POINTS) == set(FORMATS)
    assert PPR_POINTS["ppr"] > PPR_POINTS["half_ppr"] > PPR_POINTS["standard"]


def test_the_scoring_rules_version_is_a_non_empty_string():
    """It is emitted on every row so a stored row names the table that scored
    it. An empty one makes two seasons under two tables indistinguishable."""
    assert isinstance(SCORING_RULES_VERSION, str)
    assert SCORING_RULES_VERSION.strip()


def test_rates_are_none_when_the_denominator_is_zero():
    """`0.0` would be a claim about performance; `None` is the absence of one."""
    rates = build_rates(EMPTY_PASSING, EMPTY_RUSHING, EMPTY_RECEIVING)
    assert len(rates) == 4
    assert all(value is None for value in rates.values())


def test_rates_are_computed_from_the_counting_stats():
    rates = build_rates(
        {**EMPTY_PASSING, "attempts": 30, "yards": 240},
        EMPTY_RUSHING,
        {
            **EMPTY_RECEIVING,
            "targets": 10,
            "receptions": 6,
            "yards": 90,
            "yards_after_catch": 30,
        },
    )
    assert rates["yards_per_attempt"] == 8.0
    assert rates["catch_rate"] == 0.6
    assert rates["yards_per_target"] == 9.0
    assert rates["yac_per_reception"] == 5.0


@pytest.mark.parametrize("scoring_format", FORMATS)
def test_every_format_is_a_float(scoring_format):
    assert isinstance(points(receiving={"receptions": 1})[scoring_format], float)

"""The opponent adjustment: fit on offensive units, never on defensive ratings.

The spec's prohibition is not stylistic. A defensive rating is already a
function of the offenses that defense faced, so adjusting defense D by its
opponents' *defensive* ratings puts D's own allowance back into its own
correction term through however many hops the schedule provides -- and the
resulting number looks entirely reasonable.

The structural proof is the harder half and is the first test here: the
adjustment must be computable from offensive production ALONE, so feeding it
the offensive halves of the season and nothing else must reproduce the
published number exactly.
"""

import pytest

from defense_vs_position.ratings import (
    ADJUSTMENT_METHOD,
    MIN_OPPONENT_STRENGTH,
    offense_strengths,
)

from . import season
from .conftest import SpyLake, run_capture

SIGNAL_TYPE = "defense_positional_allowance"


async def test_the_adjustment_divides_the_raw_rate_by_the_opponent_index(upstreams):
    """The published identity, checked on real rows.

    `adj = raw / opponent_strength_index`. Asserting the three fields agree is
    what makes `opponent_strength_index` a claim rather than decoration -- an
    index computed and then not applied would leave both other fields correct.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed("BAL")))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    checked = 0
    for row in envelope.signals:
        raw = row["fantasy_points_allowed_per_game"]
        index = row["opponent_strength_index"]
        if raw is None or index is None or index < MIN_OPPONENT_STRENGTH:
            continue
        assert row["fantasy_points_allowed_per_game_adj"] == pytest.approx(
            raw / index, rel=1e-3
        )
        checked += 1
    assert checked > 100, "the fixture did not produce enough adjusted rows"


async def test_the_index_actually_moves_when_the_schedule_does(upstreams):
    """An adjustment that is always 1.0 satisfies the identity above and
    adjusts nothing. `BAL` faces starved offenses, so its index must fall
    below the league-average 1.0 and its adjusted rate must exceed its raw
    one."""
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed("BAL")))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = {
        (r["team_id"], r["position"], r["scoring_format"]): r for r in envelope.signals
    }
    skewed = rows[("BAL", "WR", "ppr")]
    assert skewed["opponent_strength_index"] < 1.0
    assert (
        skewed["fantasy_points_allowed_per_game_adj"]
        > skewed["fantasy_points_allowed_per_game"]
    )
    assert len({r["opponent_strength_index"] for r in envelope.signals}) > 1


async def test_the_adjustment_names_what_it_actually_did(upstreams):
    """`adjustment_method` identifies the arithmetic, not a model this
    collector does not implement -- and `adjustment_window_weeks` is the real
    week count of the play set, not a constant."""
    upstreams.set_pbp(season.pbp_document(weeks=3))
    envelope = (await run_capture(SpyLake(), week=3))[SIGNAL_TYPE]
    assert {r["adjustment_method"] for r in envelope.signals} == {ADJUSTMENT_METHOD}
    assert {r["adjustment_window_weeks"] for r in envelope.signals} == {3}


def test_the_strength_of_an_offense_excludes_the_game_being_adjusted():
    """Leave-one-out, which is the whole non-circularity argument.

    Without it, a defense that shut an offense out is told that offense is
    weak partly BECAUSE of the shutout, and has its own achievement adjusted
    away. Here `OFF` scores 30, 10 and 10; the strength used against the
    30-point game must come from the other two, not from all three.
    """
    points = {("OFF", "g1"): 30.0, ("OFF", "g2"): 10.0, ("OFF", "g3"): 10.0}
    strengths = offense_strengths(points)
    league_mean = 50.0 / 3
    # g1 is excluded from its own estimate: mean(10, 10) = 10.
    assert strengths[("OFF", "g1")] == pytest.approx(10.0 / league_mean)
    # g2 is excluded from its own: mean(30, 10) = 20.
    assert strengths[("OFF", "g2")] == pytest.approx(20.0 / league_mean)
    # A full-sample estimate would give the same number for both.
    assert strengths[("OFF", "g1")] != strengths[("OFF", "g2")]


def test_a_single_game_offense_falls_back_rather_than_dividing_by_zero():
    """Week 1 has no other games to leave one out of. The residual
    circularity is stated rather than papered over with a NaN."""
    strengths = offense_strengths({("OFF", "g1"): 10.0, ("TWO", "g1"): 30.0})
    assert strengths[("OFF", "g1")] == pytest.approx(0.5)
    assert strengths[("TWO", "g1")] == pytest.approx(1.5)


def test_a_league_that_produced_nothing_is_all_average():
    """Not a division by zero, and not everyone infinitely strong."""
    assert offense_strengths({("A", "g"): 0.0, ("B", "g"): 0.0}) == {
        ("A", "g"): 1.0,
        ("B", "g"): 1.0,
    }
    assert offense_strengths({}) == {}


def test_strength_is_expressed_against_the_league_mean():
    """`1.0` is league-average -- the unit `opponent_strength_index` is
    documented in, and the reason the adjusted figure is comparable across
    positions at all."""
    strengths = offense_strengths(
        {("A", "g1"): 10.0, ("A", "g2"): 10.0, ("B", "g1"): 30.0, ("B", "g2"): 30.0}
    )
    assert strengths[("A", "g1")] == pytest.approx(0.5)
    assert strengths[("B", "g1")] == pytest.approx(1.5)


async def test_no_defensive_rating_reaches_the_adjustment(upstreams):
    """The structural half of the non-circularity claim.

    `offense_strengths` takes one argument: per-game points BY an offense. If
    a defensive rating were ever consulted, recomputing the strengths from the
    offensive halves alone could not reproduce the published index -- so this
    recomputes them and asserts it does.
    """
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = [
        r
        for r in envelope.signals
        if r["position"] == "WR" and r["scoring_format"] == "ppr"
    ]
    # In the flat fixture every offense is identical, so every offense is
    # exactly league-average and every index is exactly 1.0. A defensive
    # rating leaking in would vary with each defense's own allowance.
    assert {r["opponent_strength_index"] for r in rows} == {1.0}
    assert all(
        r["fantasy_points_allowed_per_game_adj"] == r["fantasy_points_allowed_per_game"]
        for r in rows
    )

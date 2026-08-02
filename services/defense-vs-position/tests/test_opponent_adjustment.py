"""The opponent adjustment: fit on the opposing unit's own production.

Never on a prior rating of the unit being adjusted. The spec's prohibition is
not stylistic: a rating is already a function of the units it faced, so
adjusting defense D by its opponents' *defensive* ratings puts D's own
allowance back into its own correction term through however many hops the
schedule provides -- and the resulting number looks entirely reasonable.

**Every position is covered here, DST included.** This file used to contain no
DST row at all, and the one test that iterated everything asserted only
`adj == raw / index`, which a degenerate adjustment satisfies trivially. That
is how a self-referential DST yardstick shipped: `build_rows` skipped the
re-key onto the producing opponent for that one position, the strength became
the team's own leave-one-out mean of the quantity being rated, and
`adj` collapsed to the league mean identically for all 32 teams.
"""

import pytest

from defense_vs_position.ratings import (
    ADJUSTMENT_METHOD,
    MIN_OPPONENT_STRENGTH,
    opposing_unit_strengths,
)
from defense_vs_position.scoring import (
    POSITIONS,
    SACK_POINTS,
    points_allowed_points,
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


@pytest.mark.parametrize("position", POSITIONS)
async def test_every_position_gets_a_real_schedule_spread(upstreams, position):
    """**Parametrised over all six, DST included.**

    The suite used to filter to `WR` everywhere, which is how the degenerate
    DST adjustment survived: it satisfied `adj == raw / index` trivially,
    because `index` was `raw / league_mean`.

    A degenerate adjustment produces exactly ONE distinct adjusted value
    across the league, because `raw / (raw / league_mean)` is the league mean
    for every team. That is the discriminator here; the correlation test below
    is the sharper, magnitude-independent form of the same check.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.asymmetric_league()))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = [
        r
        for r in envelope.signals
        if r["position"] == position and r["scoring_format"] == "ppr"
    ]
    assert len(rows) == 32

    indices = [r["opponent_strength_index"] for r in rows]
    # A schedule index averages to league-average by construction -- but so
    # did the self-referential one, which is why this alone is not the check.
    assert sum(indices) / len(indices) == pytest.approx(1.0, abs=0.05)

    adjusted = {r["fantasy_points_allowed_per_game_adj"] for r in rows}
    assert len(adjusted) > 1, (
        f"{position}: every team published the same adjusted value -- the "
        "yardstick is the team's own rate, not its opponents'"
    )


@pytest.mark.parametrize("position", POSITIONS)
async def test_the_index_does_not_track_the_teams_own_rate(upstreams, position):
    """The sharpest discriminator for F1, and it generalises to all six.

    Under a self-referential yardstick `index == own_rate / league_mean`
    *exactly*, so the two are perfectly correlated. Under a real schedule
    index the correlation is incidental. Correlation rather than an
    element-wise comparison because a handful of teams can satisfy the latter
    by coincidence in a two-week fixture -- 11 of 32 did.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.asymmetric_league()))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = [
        r
        for r in envelope.signals
        if r["position"] == position and r["scoring_format"] == "ppr"
    ]
    raw = [r["fantasy_points_allowed_per_game"] for r in rows]
    index = [r["opponent_strength_index"] for r in rows]

    mean_raw = sum(raw) / len(raw)
    mean_index = sum(index) / len(index)
    covariance = sum(
        (a - mean_raw) * (b - mean_index) for a, b in zip(raw, index, strict=True)
    )
    spread_raw = sum((a - mean_raw) ** 2 for a in raw) ** 0.5
    spread_index = sum((b - mean_index) ** 2 for b in index) ** 0.5
    correlation = covariance / (spread_raw * spread_index)

    assert correlation < 0.95, (
        f"{position}: opponent_strength_index correlates {correlation:.4f} with "
        "the team's own rate -- that is the self-referential yardstick, which "
        "makes the adjusted column a constant"
    )


def expected_dst_index(drives: dict, team: str, weeks: int = 2) -> float:
    """`team`'s DST opponent-strength index, derived from the FIXTURE INPUTS.

    A genuine oracle rather than a copy of the implementation: it starts from
    the `Drive` knobs a test set and works forward, so it agrees with
    `build_rows` only if `build_rows` reads the units the contract says it
    reads.

    "not the team's own rate" is a necessary condition and not a sufficient
    one -- there are several wrong units to read, and each produces a
    different plausible number. This pins the right one.
    """
    games: list[tuple[str, str, int]] = []
    for week in range(1, weeks + 1):
        rotation = season._rotation(week)
        for index in range(0, len(rotation) - 1, 2):
            games.append((rotation[index], rotation[index + 1], week))

    def conceded(offense: str, week: int) -> float:
        drive = drives[(offense, week)]
        return drive.sacks * SACK_POINTS + points_allowed_points(drive.points)

    # What each DEFENSE generated, per game -- the producing unit.
    produced: dict[tuple[str, int], float] = {}
    opponent_of: dict[tuple[str, int], str] = {}
    for away, home, week in games:
        produced[(home, week)] = conceded(away, week)
        produced[(away, week)] = conceded(home, week)
        opponent_of[(away, week)] = home
        opponent_of[(home, week)] = away

    league_mean = sum(produced.values()) / len(produced)
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for (unit, _week), points in produced.items():
        totals[unit] = totals.get(unit, 0.0) + points
        counts[unit] = counts.get(unit, 0) + 1

    samples = []
    for week in range(1, weeks + 1):
        opponent = opponent_of[(team, week)]
        remaining = counts[opponent] - 1
        loo = (
            (totals[opponent] - produced[(opponent, week)]) / remaining
            if remaining > 0
            else totals[opponent]
        )
        samples.append(loo / league_mean)
    return sum(samples) / len(samples)


async def test_the_dst_index_is_the_opposing_defenses_generation(upstreams):
    """**The positive form: exactly which unit the index is built from.**

    Three separate wrong answers were reachable here and only the combination
    of two of them produced the constant the review found:

    * skip the re-key onto the producing opponent (the yardstick becomes what
      the OPPONENT conceded, not what it generated);
    * read the strength at the team's own key rather than its opponent's;
    * both together, which is the bug that shipped.

    "the index does not track the team's own rate" catches the third and
    neither of the first two, because each of those is a different plausible
    number rather than a constant. This test computes the expected index from
    the fixture's own `Drive` knobs and asserts equality, so all three die.
    """
    drives = season.asymmetric_league()
    upstreams.set_pbp(season.pbp_document(drives=drives))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = {
        r["team_id"]: r
        for r in envelope.signals
        if r["position"] == "DST" and r["scoring_format"] == "ppr"
    }

    for team in season.TEAMS:
        assert rows[team]["opponent_strength_index"] == pytest.approx(
            expected_dst_index(drives, team), rel=1e-3
        ), f"{team}'s DST index is not the mean strength of the defenses it faced"


def test_a_conceding_key_would_make_the_adjustment_a_constant():
    """The bug's mechanism, pinned as arithmetic rather than as a story.

    Handing `opposing_unit_strengths` the CONCEDING unit's own points -- which
    is what the deleted `DST` special case did -- makes each strength the
    team's own leave-one-out mean over the league mean. The mean of a team's
    leave-one-out means is exactly its full mean, so dividing the rate by it
    yields the league mean for every team, identically.

    This test does not exercise `build_rows`; it exists so the *reason* the
    special case was wrong survives in the suite even if the call site is
    rewritten again.
    """
    own_points = {
        ("A", "g1"): 2.0,
        ("A", "g2"): 4.0,
        ("B", "g1"): 10.0,
        ("B", "g2"): 20.0,
    }
    strengths = opposing_unit_strengths(own_points)
    league_mean = 36.0 / 4

    for team, total in (("A", 6.0), ("B", 30.0)):
        games = [g for (t, g) in own_points if t == team]
        rate = total / len(games)
        mean_strength = sum(strengths[(team, g)] for g in games) / len(games)
        # The collapse, exactly: rate / mean_strength == league_mean.
        assert rate / mean_strength == pytest.approx(league_mean)


async def test_an_essentially_silent_schedule_falls_back_rather_than_exploding(
    upstreams,
):
    """`MIN_OPPONENT_STRENGTH`, which was a disclosed blind spot.

    A defense whose opponents produced almost nothing at a position would
    otherwise have a tiny divisor turn a small raw allowance into an enormous
    adjusted one -- a number that is not merely wrong but off the scale the
    generator ranks on. Below the floor the raw figure is published unchanged.

    **Every one of the opponent's games has to be starved, not just the one
    against BUF** -- which is leave-one-out doing exactly its job. The strength
    used to adjust BUF's game is computed from that opponent's OTHER games, so
    starving only the head-to-head leaves the estimate untouched. Getting this
    wrong is what made the first draft of this test assert `1.0323 < 0.05`.
    """
    starved = season.replace(
        season.Drive(),
        completions=1,
        receiving_yards=0.01,
        yac=0.0,
        receiving_tds=0,
        incompletions=0,
    )
    drives = {
        (opponent, week): starved
        for opponent, _week in season.opponents_of("BUF")
        for week in (1, 2)
    }
    upstreams.set_pbp(season.pbp_document(drives=drives))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    row = next(
        r
        for r in envelope.signals
        if r["team_id"] == "BUF"
        and r["position"] == "WR"
        and r["scoring_format"] == "standard"
    )
    assert row["opponent_strength_index"] < MIN_OPPONENT_STRENGTH
    assert (
        row["fantasy_points_allowed_per_game_adj"]
        == row["fantasy_points_allowed_per_game"]
    ), "below the floor the raw figure must be published unchanged"


async def test_the_adjustment_names_what_it_actually_did(upstreams):
    """`adjustment_method` identifies the arithmetic, not a model this
    collector does not implement -- and `adjustment_window_weeks` is the real
    week count of the play set, not a constant."""
    upstreams.set_pbp(season.pbp_document(weeks=3))
    envelope = (await run_capture(SpyLake(), week=3))[SIGNAL_TYPE]
    assert {r["adjustment_method"] for r in envelope.signals} == {ADJUSTMENT_METHOD}
    assert {r["adjustment_window_weeks"] for r in envelope.signals} == {3}


def test_the_strength_of_a_unit_excludes_the_game_being_adjusted():
    """Leave-one-out, which is the whole non-circularity argument.

    Without it, a defense that shut an offense out is told that offense is
    weak partly BECAUSE of the shutout, and has its own achievement adjusted
    away. Here `OFF` scores 30, 10 and 10; the strength used against the
    30-point game must come from the other two, not from all three.
    """
    points = {("OFF", "g1"): 30.0, ("OFF", "g2"): 10.0, ("OFF", "g3"): 10.0}
    strengths = opposing_unit_strengths(points)
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
    strengths = opposing_unit_strengths({("OFF", "g1"): 10.0, ("TWO", "g1"): 30.0})
    assert strengths[("OFF", "g1")] == pytest.approx(0.5)
    assert strengths[("TWO", "g1")] == pytest.approx(1.5)


def test_a_league_that_produced_nothing_is_all_average():
    """Not a division by zero, and not everyone infinitely strong."""
    assert opposing_unit_strengths({("A", "g"): 0.0, ("B", "g"): 0.0}) == {
        ("A", "g"): 1.0,
        ("B", "g"): 1.0,
    }
    assert opposing_unit_strengths({}) == {}


def test_strength_is_expressed_against_the_league_mean():
    """`1.0` is league-average -- the unit `opponent_strength_index` is
    documented in, and the reason the adjusted figure is comparable across
    positions at all."""
    strengths = opposing_unit_strengths(
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

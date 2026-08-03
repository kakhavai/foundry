"""The opponent adjustment, the line-yards scale, and the continuity index.

Pure functions, so these are the cheapest place to pin the arithmetic — and
the arithmetic is where this collector's sibling shipped a column that carried
no information at all while every field was populated and plausible.
"""

import statistics

import pytest

from defensive_front.ratings import (
    ADJUSTMENT_METHOD,
    FRONT_ROTATION_SIZE,
    LINE_YARDS_CAP,
    MIN_OPPONENT_STRENGTH,
    UNITS,
    DefenseTotals,
    FrontTotals,
    OffenseGameTotals,
    _faced_strength,
    build_rows,
    continuity_index,
    line_yards,
    opponent_strengths,
)

from .conftest import Feeds, SpyLake, by_team, run_capture

# --------------------------------------------------------------------------
# The narrowed unit enum
# --------------------------------------------------------------------------


def test_only_overall_is_emitted():
    """The spec declares three. Only one is sourceable, and synthesising the
    other two from listed positions is what the spec's own adapter note rules
    out — so the enum is narrowed rather than filled in."""
    assert UNITS == ("overall",)


async def test_every_published_row_is_the_overall_unit():
    rows = await run_capture(Feeds(), lake=SpyLake())
    assert {row["unit"] for row in rows["defensive_front_strength"].signals} == {
        "overall"
    }


# --------------------------------------------------------------------------
# The line-yards weighting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("yards", "expected"),
    [
        (-3.0, -3.6),  # 120% credit behind the line
        (-1.0, -1.2),
        (0.0, 0.0),
        (2.0, 2.0),  # full credit through four
        (4.0, 4.0),
        (5.0, 4.5),  # half credit from five to ten
        (10.0, 7.0),
        (11.0, 7.0),  # nothing past ten
        (80.0, 7.0),
    ],
)
def test_the_line_yards_bands(yards, expected):
    """The Football Outsiders weighting, which `offensive-line` must import
    unchanged: the spec says a divergence corrupts the head-to-head
    differential silently rather than failing."""
    assert line_yards(yards) == pytest.approx(expected)


def test_the_cap_is_derived_from_the_bands():
    """Written down, it would survive a change to the bands and silently cap
    at the wrong value."""
    assert LINE_YARDS_CAP == line_yards(10.0)
    assert line_yards(1_000.0) == LINE_YARDS_CAP


# --------------------------------------------------------------------------
# The opponent adjustment
# --------------------------------------------------------------------------


def test_leave_one_out_excludes_the_game_being_adjusted():
    """Without it, a front that flattened an offence would be told that
    offence is weak partly BECAUSE of the flattening."""
    production = {("OFF", "g1"): 0.5, ("OFF", "g2"): 0.1, ("OFF", "g3"): 0.3}
    strengths = opponent_strengths(production)
    league_mean = statistics.fmean(production.values())
    # g1's strength is the mean of g2 and g3, not of all three.
    assert strengths[("OFF", "g1")] == pytest.approx(((0.1 + 0.3) / 2) / league_mean)
    assert strengths[("OFF", "g2")] == pytest.approx(((0.5 + 0.3) / 2) / league_mean)


def test_a_single_game_falls_back_to_that_game():
    strengths = opponent_strengths({("OFF", "g1"): 0.4, ("XXX", "g1"): 0.2})
    assert strengths[("OFF", "g1")] == pytest.approx(0.4 / 0.3)


def test_strengths_are_relative_to_the_league_mean():
    """`1.0` is average, which is the unit `opponent_pressure_strength_index`
    is documented in."""
    production = {
        (f"T{i}", f"g{j}"): 0.2 + 0.05 * i for i in range(4) for j in range(3)
    }
    strengths = opponent_strengths(production)
    assert statistics.fmean(strengths.values()) == pytest.approx(1.0)


def test_no_production_at_all_makes_everyone_average():
    strengths = opponent_strengths({("A", "g1"): 0.0, ("B", "g1"): 0.0})
    assert set(strengths.values()) == {1.0}
    assert opponent_strengths({}) == {}


def test_keying_by_the_conceding_unit_collapses_to_a_constant():
    """**The bug this whole module is arranged to prevent**, reproduced.

    `defense-vs-position` fed `opponent_strengths` the CONCEDING unit's own
    production. The mean of a unit's leave-one-out means is exactly its full
    mean, so `rate / strength` becomes `rate / (own_mean / league_mean)` and
    every unit publishes the league mean identically — while the raw values
    spanned 4x. Every field populated, every value plausible, no information.

    This test asserts the collapse HAPPENS when the mis-keying is done, which
    is what makes the variance assertions below meaningful rather than
    decorative.
    """
    own = {
        ("D1", "g1"): 0.20,
        ("D1", "g2"): 0.40,
        ("D2", "g1"): 0.60,
        ("D2", "g2"): 0.80,
    }
    strengths = opponent_strengths(own)
    season_rate = {
        "D1": statistics.fmean([own[("D1", "g1")], own[("D1", "g2")]]),
        "D2": statistics.fmean([own[("D2", "g1")], own[("D2", "g2")]]),
    }
    collapsed = []
    for unit, rate in season_rate.items():
        faced = statistics.fmean([strengths[(unit, game)] for game in ("g1", "g2")])
        collapsed.append(rate / faced)
    assert collapsed[0] == pytest.approx(collapsed[1]), (
        "the mis-keying no longer collapses, so the variance tests below no "
        "longer prove the adjustment is correctly keyed"
    )


# --------------------------------------------------------------------------
# ...and the same thing, end to end, on the real capture path
# --------------------------------------------------------------------------


async def test_the_adjusted_columns_are_not_constant():
    """**The tell.** Zero variance in an adjusted column is what a
    self-referential adjustment looks like from the outside, and it is the
    only symptom: coverage stays 1.0 and every field validates.

    The fixture is `f(offence) + g(defence)` deliberately — see
    `tests/season.py`. A fixture varying by the offence alone would have a
    CORRECT adjustment remove 100% of the variance and collapse to the league
    mean, which is bit-for-bit identical to the bug.
    """
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for column in (
        "pressure_rate_generated_adj",
        "sack_rate_generated_adj",
        "adjusted_line_yards_allowed",
        "opponent_pressure_strength_index",
    ):
        values = [row[column] for row in rows.values()]
        assert len(set(values)) > 1, f"{column} is a constant across the league"
        assert statistics.pvariance(values) > 0.0


async def test_the_adjustment_moves_teams_relative_to_the_raw_value():
    """An adjustment that equals its input has not adjusted anything, and it
    would pass every variance test above."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    moved = [
        team
        for team, row in rows.items()
        if row["pressure_rate_generated_adj"] != row["pressure_rate_generated"]
    ]
    assert len(moved) >= len(rows) - 1, moved


async def test_a_team_facing_weak_lines_is_adjusted_downward():
    """Direction, not just movement. The yardstick is pressure ALLOWED by the
    opposing offences, so a slate that concedes a lot (strength > 1) must
    deflate the rating and one that concedes little must inflate it. A sign
    flip passes every other test in this file."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for row in rows.values():
        strength = row["opponent_pressure_strength_index"]
        if strength > 1.0:
            assert row["pressure_rate_generated_adj"] < row["pressure_rate_generated"]
        elif strength < 1.0:
            assert row["pressure_rate_generated_adj"] > row["pressure_rate_generated"]


def test_a_degenerate_opponent_slate_publishes_the_raw_value():
    """Below the floor the raw figure is published unchanged rather than
    divided by something near zero, which would turn a small allowance into an
    enormous one."""
    totals = FrontTotals(
        defense={"AAA": DefenseTotals(pass_rush_snaps=10, pressures=3, carries=5)},
        offense_game={
            ("BBB", "g1"): OffenseGameTotals(pass_rush_snaps=10, pressures_allowed=10),
            ("CCC", "g1"): OffenseGameTotals(pass_rush_snaps=10, pressures_allowed=0),
        },
        opponents={("AAA", "g1"): "CCC", ("CCC", "g1"): "AAA"},
        weeks={1},
    )
    rows, _ = build_rows(totals, absences={}, degraded=(), null_field_reason={})
    row = next(r for r in rows if r["team_id"] == "AAA")
    assert row["opponent_pressure_strength_index"] < MIN_OPPONENT_STRENGTH
    assert row["pressure_rate_generated_adj"] == row["pressure_rate_generated"]


# --------------------------------------------------------------------------
# Pressure is attributed to the rushing unit, not the outcome
# --------------------------------------------------------------------------


async def test_a_pressure_is_counted_when_the_ball_came_out():
    """The spec's requirement: hurries and knockdowns count even when the ball
    is out. If pressures were filtered by outcome, `pressure_to_sack_rate`
    would be 1.0 everywhere — the pressures would BE the sacks."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for team, row in rows.items():
        assert row["pressure_to_sack_rate"] < 1.0, team
        assert row["pressure_rate_generated"] > row["sack_rate_generated"], team


async def test_pressures_outnumber_sacks_by_a_real_margin():
    """A stricter version of the same claim: not merely 'not equal' but a
    conversion rate in the range charting data actually produces. A collector
    that counted only sacks as pressure would sit at 1.0; one that counted
    every dropback would sit near the sack rate."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    conversions = [row["pressure_to_sack_rate"] for row in rows.values()]
    assert 0.05 < min(conversions)
    assert max(conversions) < 0.75


# --------------------------------------------------------------------------
# front_continuity_index
# --------------------------------------------------------------------------


def test_an_unchanged_front_scores_one():
    window = {f"p{i}": 100 for i in range(FRONT_ROTATION_SIZE)}
    assert continuity_index(window, window) == pytest.approx(1.0)


def test_a_wholly_replaced_front_scores_low():
    window = {f"old{i}": 100 for i in range(FRONT_ROTATION_SIZE)}
    window.update({f"new{i}": 5 for i in range(FRONT_ROTATION_SIZE)})
    recent = {f"new{i}": 5 for i in range(FRONT_ROTATION_SIZE)}
    assert continuity_index(window, recent) == pytest.approx(35 / (700 + 35), abs=1e-6)


def test_the_denominator_is_actual_front_participations():
    """Not `FRONT_ROTATION_SIZE x snaps`. Nickel drops a linebacker, so a snap
    carries about 6.3 front players; dividing by seven would deflate every
    team by ~10% for a reason unrelated to continuity."""
    window = {"a": 60, "b": 40}
    assert continuity_index(window, {"a": 6, "b": 4}) == pytest.approx(1.0)


def test_no_front_snaps_is_null_not_zero():
    """Which is what a missing roster feed looks like, and is a different fact
    from a front that turned over completely."""
    assert continuity_index({}, {"a": 1}) is None
    assert continuity_index({"a": 1}, {}) is None


def test_ties_in_the_rotation_are_broken_deterministically():
    """Two rotational ends on identical snap counts would otherwise enter the
    rotation depending on which game streamed first, moving the index between
    passes over nothing."""
    window = dict.fromkeys("abcdefghij", 10)
    first = continuity_index(window, dict.fromkeys("abcdefghij", 5))
    second = continuity_index(window, dict(reversed(list(window.items()))))
    assert first == second


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


async def test_every_row_names_the_adjustment_that_produced_it():
    """A consumer reading an old lake object has nothing else to tell it which
    vintage it holds."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for row in rows.values():
        assert row["adjustment_method"] == ADJUSTMENT_METHOD
        assert row["adjustment_window_weeks"] == 5


# --------------------------------------------------------------------------
# Blitz: the conditional rate, and its threshold
# --------------------------------------------------------------------------


async def test_the_blitz_rate_is_populated_and_varies():
    """`BLITZ_RUSHERS` is five OR MORE — the spec's own wording, and the
    standard definition, because four is a base rush and the fifth is the
    extra man. An off-by-one to `> 5` zeroes the column for every front that
    blitzes exactly five, which is most of them: on the real 2025 season the
    league blitz rate is 0.269 and spans 0.177 to 0.461."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    rates = [row["blitz_rate"] for row in rows.values()]
    assert all(rate > 0.0 for rate in rates), rates
    assert len(set(rates)) > 1, "every front blitzes at the same rate"
    assert max(rates) < 1.0, "every snap is a blitz"


async def test_pressure_when_blitzing_is_a_rate_not_a_count_of_blitzes():
    """A collector that counted every blitz snap as a blitz pressure publishes
    1.0 for the whole league, which is a populated, plausible, useless
    column. The real 2025 range is 0.314 to 0.516."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for team, row in rows.items():
        rate = row["pressure_rate_when_blitzing"]
        assert rate is not None, team
        assert 0.0 < rate < 1.0, (team, rate)


async def test_the_blitz_pressure_rate_is_consistent_with_the_overall_one():
    """A blitz pressure is a pressure, so the conditional rate cannot exceed
    what the whole sample could support. Ties the two columns together rather
    than checking each in isolation."""
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for team, row in rows.items():
        blitz_snaps = row["blitz_rate"] * row["pass_rush_snaps"]
        blitz_pressures = row["pressure_rate_when_blitzing"] * blitz_snaps
        all_pressures = row["pressure_rate_generated"] * row["pass_rush_snaps"]
        assert blitz_pressures <= all_pressures + 1e-6, team


async def test_pressure_to_sack_is_sacks_over_PRESSURES_not_over_dropbacks():
    """Two rates share a numerator here, and dividing by the wrong denominator
    produces a plausible number rather than an error. Pinned by the identity
    that ties the three columns together: `sack_rate = pressure_rate x
    pressure_to_sack_rate` holds only when the conversion is over pressures.
    """
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for team, row in rows.items():
        assert row["sack_rate_generated"] == pytest.approx(
            row["pressure_rate_generated"] * row["pressure_to_sack_rate"],
            abs=2e-4,
        ), team


async def test_every_rate_divides_by_its_own_declared_denominator():
    """**The invariant that catches a swapped denominator.**

    Every published rate is a count over a sample size that is also published,
    so `rate x denominator` must come back to a whole number of events. Swap
    a denominator — run stuffs over dropbacks, pressures over carries — and
    the value stays in range, stays plausible, validates against the schema,
    and stops being an integer count.
    """
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    for team, row in rows.items():
        for rate, denominator in (
            ("pressure_rate_generated", "pass_rush_snaps"),
            ("sack_rate_generated", "pass_rush_snaps"),
            ("blitz_rate", "pass_rush_snaps"),
            ("run_stuff_rate_generated", "run_defense_snaps"),
        ):
            events = row[rate] * row[denominator]
            assert events == pytest.approx(round(events), abs=5e-3), (
                team,
                rate,
                denominator,
                events,
            )


async def test_the_run_metrics_are_recomputed_from_the_fixture_plays():
    """The run columns, checked against the plays that produced them.

    The `rate x denominator` invariant above does not catch a swapped run
    denominator on its own: this fixture's snaps and carries are 120 and 90, a
    ratio of 4/3, so a stuff count divisible by four still comes back integral.
    **That is a degenerate-fixture escape, and the fix is to compare against
    the source rather than to a self-consistency property.**
    """
    feeds = Feeds()
    rows = by_team(await run_capture(feeds, lake=SpyLake()))
    for team, row in rows.items():
        carries = [
            play for play in feeds.built.plays if play.rush and play.defense == team
        ]
        stuffs = [play for play in carries if play.rushing_yards <= 0]
        assert row["run_defense_snaps"] == len(carries), team
        assert row["run_stuff_rate_generated"] == pytest.approx(
            len(stuffs) / len(carries), abs=1e-4
        ), team
        # ...and the run sample is genuinely a different size from the pass
        # one, so a swapped denominator has somewhere to show up.
        assert row["run_defense_snaps"] != row["pass_rush_snaps"], team


async def test_the_pass_metrics_are_recomputed_from_the_fixture_plays():
    """The same discipline on the pressure side, against the charted plays."""
    feeds = Feeds()
    rows = by_team(await run_capture(feeds, lake=SpyLake()))
    for team, row in rows.items():
        snaps = [
            play
            for play in feeds.built.plays
            if play.rushers > 0 and play.defense == team
        ]
        pressures = [play for play in snaps if play.was_pressure]
        sacks = [play for play in snaps if play.sack]
        blitzes = [play for play in snaps if play.rushers >= 5]
        assert row["pass_rush_snaps"] == len(snaps), team
        assert row["pressure_rate_generated"] == pytest.approx(
            len(pressures) / len(snaps), abs=1e-4
        ), team
        assert row["sack_rate_generated"] == pytest.approx(
            len(sacks) / len(snaps), abs=1e-4
        ), team
        assert row["blitz_rate"] == pytest.approx(
            len(blitzes) / len(snaps), abs=1e-4
        ), team
        assert row["pressure_to_sack_rate"] == pytest.approx(
            len(sacks) / len(pressures), abs=1e-4
        ), team


# --------------------------------------------------------------------------
# ...and HOW the slate is aggregated into one number
# --------------------------------------------------------------------------


def test_the_faced_strength_is_the_arithmetic_mean_of_the_slate():
    """**The aggregator itself, pinned.**

    `_faced_strength` turns a defence's slate of opponent strengths into the
    single number every `_adj` column is divided by. Nothing used to constrain
    which aggregation that is: `max`, `min`, `median` and `samples[0]` all
    survived the whole suite, because each of them still produces a
    non-degenerate, plausible, correctly-signed column. Only removing the
    adjustment entirely died — so the suite proved *an* adjustment happened
    and never *which*.

    On real 2025 data swapping `fmean` for `max` moves the strength index from
    a mean of 1.0000 to 1.2173 and deflates every published `_adj` by ~18%,
    while the variance gauge, `collector_coverage_ratio` and the timing guard
    all stay green. The spec is explicit that a scale divergence from
    `offensive-line` "silently corrupts the differential rather than failing";
    this is exactly that.

    The slate below is asymmetric on purpose, so the mean is distinct from the
    median, the extremes and the first element — a symmetric one would leave
    the median alive.
    """
    strengths = {("O1", "g1"): 0.5, ("O2", "g2"): 0.7, ("O3", "g3"): 1.8}
    opponents = {("D", "g1"): "O1", ("D", "g2"): "O2", ("D", "g3"): "O3"}
    faced = _faced_strength("D", strengths, opponents)
    assert faced == pytest.approx(1.0)
    assert faced != pytest.approx(max(strengths.values()))
    assert faced != pytest.approx(min(strengths.values()))
    assert faced != pytest.approx(statistics.median(strengths.values()))


def test_an_unplayed_slate_is_average_rather_than_an_error():
    """No comparable opponent means no correction, not a crash and not a
    zero — dividing by zero strength would make an unseen defence infinite."""
    assert _faced_strength("D", {}, {}) == 1.0
    assert _faced_strength("D", {("O1", "g1"): 2.0}, {("OTHER", "g1"): "O1"}) == 1.0


async def test_the_strength_index_averages_one_across_the_league():
    """**The invariant that catches a swapped aggregator at the published
    level**, and the one the README states in prose but nothing encoded.

    Leave-one-out strengths are ratios against the league mean, so the mean of
    a balanced league's faced-strength indices is 1.0 by construction. `max`
    reads 1.159, `min` 0.766, `median` 1.034 and first-game 0.999 on this
    fixture — every one of them a silently rescaled `_adj` column.
    """
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    index = [row["opponent_pressure_strength_index"] for row in rows.values()]
    assert statistics.fmean(index) == pytest.approx(1.0, abs=1e-4), index
    # ...and it is a real slate rather than every team facing an average one.
    assert min(index) < 0.99 < 1.01 < max(index)

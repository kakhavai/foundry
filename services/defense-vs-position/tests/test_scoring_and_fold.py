"""Fantasy scoring, and that both rate bases come from one play set.

The spec: "It must emit both the per-game and per-opportunity basis from the
same underlying play set." Two independently filtered folds would be two
different statistics that happen to share a name, and the disagreement would
show up as exactly the rank divergence the guard flags -- for a reason the
guard's operator could never diagnose.

The strongest available proof is arithmetic identity: with one accumulator,
`per_game * games == per_opportunity * opportunities` for every row, because
both are the same numerator. Two folds cannot satisfy that except by accident.
"""

import pytest

from defense_vs_position.scoring import (
    ALIGNMENTS,
    PLAYER_POSITIONS,
    POSITIONS,
    RECEPTION_POINTS,
    SACK_POINTS,
    SCORING_FORMATS,
    DstLine,
    StatLine,
    field_goal_points,
    points_allowed_points,
)

from . import season
from .conftest import SpyLake, run_capture

SIGNAL_TYPE = "defense_positional_allowance"


async def test_both_bases_share_one_numerator(upstreams):
    """`per_game * games == per_opportunity * opportunities`, every row.

    This is what "one play set" means operationally. A second, independently
    filtered fetch could not hold it.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed("BAL")))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    checked = 0
    for row in envelope.signals:
        ppg = row["fantasy_points_allowed_per_game"]
        ppo = row["fantasy_points_allowed_per_opportunity"]
        if ppg is None or ppo is None:
            continue
        assert ppg * row["games_sampled"] == pytest.approx(
            ppo * row["opportunities_defended"], rel=1e-3
        )
        checked += 1
    assert checked > 300, "the fixture did not produce enough comparable rows"


async def test_the_scoring_format_changes_only_the_reception_weight(upstreams):
    """PPR minus standard is exactly one point per reception. Anything else
    means a weight leaked into the format switch."""
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = {
        (r["team_id"], r["position"], r["scoring_format"]): r for r in envelope.signals
    }
    for team in season.TEAMS:
        for position in ("RB", "WR", "TE"):
            standard = rows[(team, position, "standard")]
            ppr = rows[(team, position, "ppr")]
            half = rows[(team, position, "half-ppr")]
            receptions = standard["receptions_allowed_per_game"]
            assert ppr["fantasy_points_allowed_per_game"] - standard[
                "fantasy_points_allowed_per_game"
            ] == pytest.approx(receptions)
            assert half["fantasy_points_allowed_per_game"] - standard[
                "fantasy_points_allowed_per_game"
            ] == pytest.approx(receptions * 0.5)


async def test_the_k_and_dst_rows_are_identical_across_formats(upstreams):
    """No kicking or team-defense weight varies by format. Stated in the
    schema, so it is asserted rather than left for a reader to assume the
    three rows differ."""
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    for position in ("K", "DST"):
        for team in season.TEAMS:
            values = {
                r["fantasy_points_allowed_per_game"]
                for r in envelope.signals
                if r["team_id"] == team and r["position"] == position
            }
            assert len(values) == 1, f"{team}/{position} varies by scoring format"


async def test_rush_yards_per_carry_is_rb_only(upstreams):
    """Spec: "Populated for RB alignments; null otherwise."

    **Driven with a quarterback who actually runs.** Without one, every
    non-RB position has zero carries, `_rate` returns `None` for a zero
    denominator anyway, and dropping the `position == "RB"` guard changes no
    output at all -- the assertion below passes against code that has no
    guard. A QB scramble is the fixture that makes the guard observable, and
    scrambles are not exotic: they are on every real play-by-play.
    """
    scramble = season.Play(
        game_id="2026_01_ARI_ATL",
        week=1,
        posteam="ARI",
        defteam="ATL",
        home_team="ATL",
        away_team="ARI",
        home_score=20,
        away_score=20,
        play_type="run",
        values={
            "rusher_player_id": season.gsis_id("ARI", "QB"),
            "rushing_yards": 9.0,
        },
    ).to_row()
    upstreams.set_pbp(season.pbp_document(extra_rows=[scramble]))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    rows = {
        (r["team_id"], r["position"], r["scoring_format"]): r for r in envelope.signals
    }
    scrambled = rows[("ATL", "QB", "ppr")]
    assert scrambled["rush_yards_allowed_per_carry"] is None, (
        "the RB-only guard was dropped: a quarterback with carries is "
        "reporting a rushing rate"
    )
    # The scramble is still a real opportunity worth real points, so this is
    # a field-scoping assertion rather than the carry being ignored.
    assert (
        scrambled["fantasy_points_allowed_per_game"]
        > rows[("ATL", "QB", "ppr")]["fantasy_points_allowed_per_opportunity"]
    )

    for row in envelope.signals:
        if row["position"] == "RB":
            assert row["rush_yards_allowed_per_carry"] == pytest.approx(4.0)
        else:
            assert row["rush_yards_allowed_per_carry"] is None


async def test_receiving_fields_are_null_for_k_and_dst(upstreams):
    """`None`, not `0.0`. A generator averaging a structural zero would rate
    an inapplicable split as an elite matchup."""
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    for row in envelope.signals:
        applicable = row["position"] in {"QB", "RB", "WR", "TE"}
        for field in (
            "targets_allowed_per_game",
            "receptions_allowed_per_game",
            "receiving_yards_allowed_per_game",
        ):
            assert (row[field] is not None) is applicable, (row["position"], field)


def expected_dst_points(drives: dict, team: str, weeks: int = 2) -> float:
    """What `team` should concede per game, computed from the fixture itself.

    Deliberately recomputed from the `Drive` knobs rather than hard-coded, so
    the assertion is "the pipeline agrees with the input" rather than "the
    pipeline agrees with a number somebody typed once".
    """
    total = 0.0
    for week in range(1, weeks + 1):
        drive = drives[(team, week)]
        total += drive.sacks * SACK_POINTS + points_allowed_points(drive.points)
    return total / weeks


async def test_the_dst_row_is_built_from_the_conceding_teams_own_game(upstreams):
    """The one place `team_id` cannot mean "the defense the row describes",
    pinned on a fixture that can actually tell the two answers apart.

    **This test was vacuous before.** The flat fixture gave every team one
    sack and 20 points, so keying a DST row by the OPPOSING defense instead of
    the conceding team produced byte-identical output and the mutant survived
    the whole suite. `asymmetric_league` gives every team a distinct sack count
    and score, so each team's row is now a value only its own game can produce.
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
        assert rows[team]["fantasy_points_allowed_per_game"] == pytest.approx(
            expected_dst_points(drives, team)
        ), f"{team}'s DST row was not built from {team}'s own concessions"
        # Its denominator is offensive plays run, not anything defensive.
        assert rows[team]["opportunities_defended"] > 0

    # And the population is genuinely discriminating: if every team conceded
    # the same thing, the assertion above would pass under the inversion too.
    distinct = {r["fantasy_points_allowed_per_game"] for r in rows.values()}
    assert len(distinct) > 8, (
        "the fixture is not asymmetric enough to distinguish the two answers"
    )


async def test_points_allowed_is_read_off_the_conceding_teams_own_side(upstreams):
    """`home_score` for the home team, `away_score` for the away team.

    Every game in the flat fixture is 20-20, so swapping the two branches in
    `_fold_team_defense` was a no-op across the entire suite while moving real
    2025 numbers by up to 1.6 points a game. The points-allowed tier is the
    largest single term in `DstLine.fantasy_points`, so this is not a
    rounding-level gap.
    """
    drives = season.asymmetric_league()
    upstreams.set_pbp(season.pbp_document(drives=drives))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = {
        r["team_id"]: r
        for r in envelope.signals
        if r["position"] == "DST" and r["scoring_format"] == "ppr"
    }

    checked = 0
    for week in (1, 2):
        rotation = season._rotation(week)
        for index in range(0, len(rotation) - 1, 2):
            away, home = rotation[index], rotation[index + 1]
            # The fixture guarantees the two sides of every game differ, which
            # is the property that makes the branch observable at all.
            assert drives[(away, week)].points != drives[(home, week)].points
            checked += 1
    assert checked == 32, "expected 16 games a week over two weeks"

    # Each team's tier must follow its OWN score. Swapping the branches gives
    # each team its opponent's tier, which these two teams disagree about.
    for team in season.TEAMS:
        own = expected_dst_points(drives, team)
        assert rows[team]["fantasy_points_allowed_per_game"] == pytest.approx(own)


async def test_the_dst_adjustment_is_not_a_constant(upstreams):
    """**The bug this test exists for shipped once.**

    `DST` lines are keyed by the CONCEDING team, exactly as player-position
    lines are keyed by the conceding defense, so both must be re-keyed onto the
    PRODUCING opponent before the strength is computed. `build_rows` used to
    special-case DST and skip that re-key, which made the yardstick the team's
    own leave-one-out mean of the very quantity being rated. The mean of a
    team's leave-one-out means is exactly its full mean, so

        adjusted == ppg / (ppg / league_mean) == league_mean

    identically, for every team. On the real 2025 season all 32 DST rows
    published `adj = 5.925` while raw spanned 2.588 to 10.471 -- 96 of 576 rows
    whose adjusted column carried no information at all, under a schema that
    describes it as opponent-adjusted.

    Nothing caught it because the adjustment suite contained no DST row and the
    one test that iterated everything asserted only `adj == raw / index`, which
    the degenerate case satisfies trivially.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.asymmetric_league()))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = [
        r
        for r in envelope.signals
        if r["position"] == "DST" and r["scoring_format"] == "ppr"
    ]

    adjusted = {r["fantasy_points_allowed_per_game_adj"] for r in rows}
    assert len(adjusted) > 1, (
        "every DST row published the same adjusted value -- the adjustment is "
        "self-referential again"
    )
    # Sharper than "not all equal", and a direct measure rather than an
    # invented ratio: a degenerate adjustment yields exactly ONE distinct
    # value across the league.
    assert len(adjusted) >= 8, (
        f"only {len(adjusted)} distinct adjusted values across 32 teams"
    )
    adj = [r["fantasy_points_allowed_per_game_adj"] for r in rows]
    assert max(adj) - min(adj) > 0.0

    # And the index must be an OPPONENT's strength, so it cannot simply track
    # the team's own rate -- which is exactly what the degenerate version did.
    assert len({r["opponent_strength_index"] for r in rows}) > 1


# --------------------------------------------------------------------------
# The pure arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("distance", "points"),
    [(19.0, 3.0), (39.0, 3.0), (40.0, 4.0), (49.0, 4.0), (50.0, 5.0), (63.0, 5.0)],
)
def test_field_goal_bands(distance, points):
    """Boundaries at 40 and 50 exactly, which is where an inclusive/exclusive
    slip produces a plausible number rather than an error."""
    assert field_goal_points(distance) == points


@pytest.mark.parametrize(
    ("allowed", "points"),
    [
        (0, 10.0),
        (1, 7.0),
        (6, 7.0),
        (7, 4.0),
        (13, 4.0),
        (14, 1.0),
        (20, 1.0),
        (21, 0.0),
        (27, 0.0),
        (28, -1.0),
        (34, -1.0),
        (35, -4.0),
        (70, -4.0),
    ],
)
def test_points_allowed_tiers(allowed, points):
    """Every tier boundary, both sides. The ladder is not linear, which is why
    `DstLine` stores a score per game rather than a season total."""
    assert points_allowed_points(allowed) == points


def test_a_stat_line_scores_each_component_once():
    line = StatLine(
        games={"g"},
        opportunities=10,
        receptions=4,
        receiving_yards=50.0,
        receiving_tds=1,
        carries=3,
        rushing_yards=20.0,
        rushing_tds=1,
        passing_yards=100.0,
        passing_tds=2,
        interceptions=1,
        fumbles_lost=1,
        two_point_conversions=1,
    )
    # 4 rec (ppr) + 7.0 yards + 12 TD + 4.0 pass yards + 8 pass TD - 2 INT
    # - 2 fumble + 2 two-point
    assert line.fantasy_points("ppr") == pytest.approx(33.0)
    assert line.fantasy_points("standard") == pytest.approx(29.0)
    assert line.fantasy_points("half-ppr") == pytest.approx(31.0)


def test_merging_two_lines_sums_counts_and_unions_games():
    """Counts, never rates. A mean of per-week rates overweights a defense's
    low-snap games, and a blowout is exactly the week whose snap count differs
    most."""
    first = StatLine(games={"a"}, opportunities=3, receptions=2, field_goals=[40.0])
    second = StatLine(games={"b"}, opportunities=5, receptions=1, field_goals=[20.0])
    first.merge(second)
    assert first.games == {"a", "b"}
    assert first.opportunities == 8
    assert first.receptions == 3
    assert sorted(first.field_goals) == [20.0, 40.0]


def test_a_dst_line_rebands_per_game_rather_than_on_a_season_total():
    """Two 0-point games score 20, not the 35+ tier's -4 on a zero total."""
    line = DstLine(games={"a", "b"}, points_scored={"a": 0, "b": 0})
    assert line.fantasy_points("ppr") == pytest.approx(20.0)
    merged = DstLine(games={"a"}, points_scored={"a": 40})
    merged.merge(DstLine(games={"b"}, points_scored={"b": 0}))
    assert merged.fantasy_points("ppr") == pytest.approx(-4.0 + 10.0)


def test_a_dst_line_ignores_the_scoring_format():
    line = DstLine(games={"a"}, sacks=3, points_scored={"a": 10})
    assert len({line.fantasy_points(fmt) for fmt in SCORING_FORMATS}) == 1


def test_the_reception_weights_are_the_published_ones():
    assert RECEPTION_POINTS == {"standard": 0.0, "half-ppr": 0.5, "ppr": 1.0}


def test_the_dimension_tuples_are_what_the_spec_declares():
    """`alignment` is one element long and that is the narrowing. The other
    two are the spec's enums in full."""
    assert POSITIONS == ("QB", "RB", "WR", "TE", "K", "DST")
    assert PLAYER_POSITIONS == ("QB", "RB", "WR", "TE", "K")
    assert ALIGNMENTS == ("all",)
    assert SCORING_FORMATS == ("standard", "half-ppr", "ppr")

"""The wire-format edges: rows the ordinary season fixture never produces.

Interceptions, lost fumbles, two-point conversions, safeties, defensive
touchdowns, postseason rows, unparseable weeks and unmapped roster codes are
each one branch in an adapter, and each one is a plausible-looking wrong
number rather than an error if the branch is wrong. The season fixture is
deliberately boring so those branches are absent from it; they are driven here
one at a time.
"""

import gzip

import httpx
import pytest
import respx

from defense_vs_position.adapters import pbp, players
from defense_vs_position.adapters.pbp import PbpFold, totals_from_fold
from defense_vs_position.adapters.players import PlayerRef

from . import season
from .conftest import SEASON, SpyLake, run_capture

SIGNAL_TYPE = "defense_positional_allowance"
OFFENSE, DEFENSE = "ARI", "ATL"


def extra_play(**values) -> dict:
    """One hand-built play in week 1 of the ARI @ ATL game the fixture makes."""
    play_type = values.pop("_type", "pass")
    return season.Play(
        game_id="2026_01_ARI_ATL",
        week=1,
        posteam=OFFENSE,
        defteam=DEFENSE,
        home_team="ATL",
        away_team="ARI",
        home_score=20,
        away_score=20,
        play_type=play_type,
        values=values,
    ).to_row()


async def rows_from(extra: list[dict], upstreams) -> dict:
    upstreams.set_pbp(season.pbp_document(extra_rows=extra))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    return {
        (r["team_id"], r["position"], r["scoring_format"]): r for r in envelope.signals
    }


async def test_an_interception_costs_the_quarterback_two(upstreams):
    baseline = await rows_from([], upstreams)
    thrown = await rows_from(
        [
            extra_play(
                passer_player_id=season.gsis_id(OFFENSE, "QB"),
                receiver_player_id=season.gsis_id(OFFENSE, "WR"),
                interception=1,
            )
        ],
        upstreams,
    )
    before = baseline[(DEFENSE, "QB", "ppr")]
    after = thrown[(DEFENSE, "QB", "ppr")]
    # -2 for the pick, spread over the same two games.
    assert after["fantasy_points_allowed_per_game"] == pytest.approx(
        before["fantasy_points_allowed_per_game"] - 1.0
    )
    # And the opposing DST gains the takeaway, charged to ARI's offense.
    assert thrown[(OFFENSE, "DST", "ppr")][
        "fantasy_points_allowed_per_game"
    ] == pytest.approx(
        baseline[(OFFENSE, "DST", "ppr")]["fantasy_points_allowed_per_game"] + 1.0
    )


async def test_a_lost_fumble_is_charged_to_the_fumbler(upstreams):
    baseline = await rows_from([], upstreams)
    fumbled = await rows_from(
        [
            extra_play(
                _type="run",
                rusher_player_id=season.gsis_id(OFFENSE, "RB"),
                fumble_lost=1,
                fumbled_1_player_id=season.gsis_id(OFFENSE, "RB"),
                fumbled_1_team=OFFENSE,
            )
        ],
        upstreams,
    )
    assert fumbled[(DEFENSE, "RB", "ppr")][
        "fantasy_points_allowed_per_game"
    ] == pytest.approx(
        baseline[(DEFENSE, "RB", "ppr")]["fantasy_points_allowed_per_game"] - 1.0
    )


async def test_a_defenders_fumble_is_not_charged_to_the_offense(upstreams):
    """`fumbled_1_team` is the guard. A defender who fumbles a return must not
    dock the skill players of the offense he took it from -- and the row looks
    entirely normal if he does."""
    baseline = await rows_from([], upstreams)
    returned = await rows_from(
        [
            extra_play(
                _type="run",
                rusher_player_id=season.gsis_id(OFFENSE, "RB"),
                fumble_lost=1,
                fumbled_1_player_id=season.gsis_id(OFFENSE, "RB"),
                fumbled_1_team=DEFENSE,
            )
        ],
        upstreams,
    )
    assert (
        returned[(DEFENSE, "RB", "ppr")]["fantasy_points_allowed_per_game"]
        > baseline[(DEFENSE, "RB", "ppr")]["fantasy_points_allowed_per_game"] - 1.0
    )


async def test_a_two_point_conversion_scores_two_and_no_yardage(upstreams):
    """A two-point play carries no passing or receiving yardage in fantasy
    scoring, and counting its yards would inflate every rate that touches it."""
    baseline = await rows_from([], upstreams)
    converted = await rows_from(
        [
            extra_play(
                passer_player_id=season.gsis_id(OFFENSE, "QB"),
                receiver_player_id=season.gsis_id(OFFENSE, "WR"),
                complete_pass=1,
                receiving_yards=3.0,
                passing_yards=3.0,
                two_point_attempt=1,
                two_point_conv_result="success",
            )
        ],
        upstreams,
    )
    receiver_before = baseline[(DEFENSE, "WR", "ppr")]
    receiver_after = converted[(DEFENSE, "WR", "ppr")]
    assert receiver_after["fantasy_points_allowed_per_game"] == pytest.approx(
        receiver_before["fantasy_points_allowed_per_game"] + 1.0
    )
    # No target, no reception, no yards -- only the two points.
    assert (
        receiver_after["targets_allowed_per_game"]
        == receiver_before["targets_allowed_per_game"]
    )
    assert (
        receiver_after["receiving_yards_allowed_per_game"]
        == receiver_before["receiving_yards_allowed_per_game"]
    )
    # It IS an opportunity the defense defended.
    assert (
        receiver_after["opportunities_defended"]
        == receiver_before["opportunities_defended"] + 1
    )


async def test_a_rushed_two_point_conversion_scores_two(upstreams):
    """The rushing arm of the same branch. Passing and rushing conversions are
    separate code paths and each is two points to a different position, so a
    fixture for one leaves the other uncovered."""
    baseline = await rows_from([], upstreams)
    converted = await rows_from(
        [
            extra_play(
                _type="run",
                rusher_player_id=season.gsis_id(OFFENSE, "RB"),
                rushing_yards=2.0,
                two_point_attempt=1,
                two_point_conv_result="success",
            )
        ],
        upstreams,
    )
    before = baseline[(DEFENSE, "RB", "ppr")]
    after = converted[(DEFENSE, "RB", "ppr")]
    assert after["fantasy_points_allowed_per_game"] == pytest.approx(
        before["fantasy_points_allowed_per_game"] + 1.0
    )
    # Not a carry, so `rush_yards_allowed_per_carry` must not move.
    assert (
        after["rush_yards_allowed_per_carry"] == before["rush_yards_allowed_per_carry"]
    )


async def test_a_failed_two_point_conversion_scores_nothing(upstreams):
    baseline = await rows_from([], upstreams)
    failed = await rows_from(
        [
            extra_play(
                passer_player_id=season.gsis_id(OFFENSE, "QB"),
                receiver_player_id=season.gsis_id(OFFENSE, "WR"),
                two_point_attempt=1,
                two_point_conv_result="failure",
            )
        ],
        upstreams,
    )
    assert failed[(DEFENSE, "WR", "ppr")][
        "fantasy_points_allowed_per_game"
    ] == pytest.approx(
        baseline[(DEFENSE, "WR", "ppr")]["fantasy_points_allowed_per_game"]
    )


async def test_a_pick_six_and_a_safety_score_for_the_opposing_dst(upstreams):
    baseline = await rows_from([], upstreams)
    scored = await rows_from(
        [
            extra_play(
                passer_player_id=season.gsis_id(OFFENSE, "QB"),
                interception=1,
                touchdown=1,
                td_team=DEFENSE,
            ),
            extra_play(_type="run", safety=1),
        ],
        upstreams,
    )
    # 2 (takeaway) + 6 (defensive TD) + 2 (safety) = 10, over two games.
    assert scored[(OFFENSE, "DST", "ppr")][
        "fantasy_points_allowed_per_game"
    ] == pytest.approx(
        baseline[(OFFENSE, "DST", "ppr")]["fantasy_points_allowed_per_game"] + 5.0
    )


async def test_an_offensive_touchdown_is_not_a_defensive_one(upstreams):
    """`td_team == offense` is the discriminator, and getting it backwards
    hands the opposing DST six points for every offensive score."""
    baseline = await rows_from([], upstreams)
    scored = await rows_from(
        [
            extra_play(
                _type="run",
                rusher_player_id=season.gsis_id(OFFENSE, "RB"),
                rushing_yards=5.0,
                rush_touchdown=1,
                touchdown=1,
                td_team=OFFENSE,
            )
        ],
        upstreams,
    )
    assert scored[(OFFENSE, "DST", "ppr")][
        "fantasy_points_allowed_per_game"
    ] == pytest.approx(
        baseline[(OFFENSE, "DST", "ppr")]["fantasy_points_allowed_per_game"]
    )


async def test_a_missed_field_goal_is_an_opportunity_worth_nothing(upstreams):
    baseline = await rows_from([], upstreams)
    missed = await rows_from(
        [
            extra_play(
                _type="field_goal",
                kicker_player_id=season.gsis_id(OFFENSE, "K"),
                field_goal_attempt=1,
                field_goal_result="missed",
                kick_distance=55.0,
            )
        ],
        upstreams,
    )
    before = baseline[(DEFENSE, "K", "ppr")]
    after = missed[(DEFENSE, "K", "ppr")]
    assert after["fantasy_points_allowed_per_game"] == pytest.approx(
        before["fantasy_points_allowed_per_game"]
    )
    assert after["opportunities_defended"] == before["opportunities_defended"] + 1


async def test_a_missed_extra_point_is_an_opportunity_worth_nothing(upstreams):
    baseline = await rows_from([], upstreams)
    missed = await rows_from(
        [
            extra_play(
                _type="extra_point",
                kicker_player_id=season.gsis_id(OFFENSE, "K"),
                extra_point_attempt=1,
                extra_point_result="failed",
            )
        ],
        upstreams,
    )
    before = baseline[(DEFENSE, "K", "ppr")]
    after = missed[(DEFENSE, "K", "ppr")]
    assert after["fantasy_points_allowed_per_game"] == pytest.approx(
        before["fantasy_points_allowed_per_game"]
    )
    assert after["opportunities_defended"] == before["opportunities_defended"] + 1


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"season_type": "POST"}, id="postseason"),
        pytest.param({"week": "NA"}, id="unparseable-week"),
        pytest.param({"posteam": ""}, id="no-possession"),
        pytest.param({"game_id": ""}, id="no-game"),
    ],
)
async def test_a_row_that_is_not_a_regular_season_play_is_skipped(upstreams, override):
    """Each of these produces a plausible extra row rather than an error if
    its guard is dropped -- a postseason game silently inflating a season's
    per-game rates is the one that would survive review."""
    baseline = await rows_from([], upstreams)
    play = extra_play(
        passer_player_id=season.gsis_id(OFFENSE, "QB"),
        receiver_player_id=season.gsis_id(OFFENSE, "WR"),
        complete_pass=1,
        receiving_yards=99.0,
        passing_yards=99.0,
    )
    play.update({key: str(value) for key, value in override.items()})
    unchanged = await rows_from([play], upstreams)
    assert (
        unchanged[(DEFENSE, "WR", "ppr")]["fantasy_points_allowed_per_game"]
        == baseline[(DEFENSE, "WR", "ppr")]["fantasy_points_allowed_per_game"]
    )


async def test_a_player_with_no_roster_row_is_never_an_opportunity(upstreams):
    """`positions` is the gate at parse time -- rule 2, filter as you parse.
    A punter's carry on a fake is real and belongs to nobody's matchup."""
    baseline = await rows_from([], upstreams)
    unknown = await rows_from(
        [extra_play(_type="run", rusher_player_id="00-9999999", rushing_yards=40.0)],
        upstreams,
    )
    for position in ("QB", "RB", "WR", "TE", "K"):
        assert (
            unknown[(DEFENSE, position, "ppr")]["opportunities_defended"]
            == baseline[(DEFENSE, position, "ppr")]["opportunities_defended"]
        )


# --------------------------------------------------------------------------
# The roster map
# --------------------------------------------------------------------------


async def test_a_fullback_is_scored_as_a_running_back(upstreams):
    """`ROSTER_TO_FANTASY` folds `FB` into `RB`. A code it does not carry --
    every defensive and line position -- is dropped at parse time."""
    upstreams.set_players(
        season.players_document(positions={season.gsis_id(OFFENSE, "RB"): "FB"})
    )
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    row = next(
        r
        for r in envelope.signals
        if r["team_id"] == DEFENSE
        and r["position"] == "RB"
        and r["scoring_format"] == "ppr"
    )
    assert row["games_sampled"] == 2


async def test_a_position_outside_the_fantasy_map_is_dropped(upstreams):
    upstreams.set_players(
        season.players_document(positions={season.gsis_id(OFFENSE, "RB"): "CB"})
    )
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    row = next(
        r
        for r in envelope.signals
        if r["team_id"] == DEFENSE
        and r["position"] == "RB"
        and r["scoring_format"] == "ppr"
    )
    assert row["games_sampled"] == 1, "ARI's back should no longer be an RB"


@pytest.mark.parametrize(
    ("jersey", "expected"),
    [("18", 18), ("18.0", 18), ("", None), ("NA", None)],
)
async def test_a_jersey_number_is_parsed_or_dropped(jersey, expected):
    """`jersey_number` carries 0.20 of `player-identity`'s scoring weight, and
    the feed writes it as a float. A `NA` must become `None` rather than
    raising mid-stream and failing an otherwise healthy pass."""
    body = gzip.compress(
        (
            "gsis_id,display_name,position,jersey_number,latest_team\n"
            f"00-0011000,A Receiver,WR,{jersey},PHI\n"
        ).encode()
    )
    with respx.mock:
        respx.get(players.UPSTREAM_URL).mock(
            return_value=httpx.Response(200, content=body)
        )
        async with httpx.AsyncClient() as http:
            found = await players.fetch_positions(client=http)
    assert found["00-0011000"].jersey_number == expected


async def test_a_row_with_no_gsis_id_is_skipped():
    body = gzip.compress(
        (
            "gsis_id,display_name,position,jersey_number,latest_team\n"
            ",Nobody,WR,1,PHI\n"
            "00-0011000,A Receiver,WR,1,\n"
        ).encode()
    )
    with respx.mock:
        respx.get(players.UPSTREAM_URL).mock(
            return_value=httpx.Response(200, content=body)
        )
        async with httpx.AsyncClient() as http:
            found = await players.fetch_positions(client=http)
    assert set(found) == {"00-0011000"}
    assert found["00-0011000"].team is None, "an empty team must not become ''"


def test_the_source_refs_name_the_real_artifacts():
    assert pbp.source_ref(SEASON).endswith("pbp/play_by_play_2026.csv.gz")
    assert players.source_ref().endswith("players/players.csv.gz")


def test_an_unresolved_player_is_dropped_by_totals_from_fold():
    """The gate, in isolation from HTTP. `resolved` is a set of GSIS ids and
    a player absent from it contributes nothing at all."""
    fold = PbpFold()
    fold.line("DEF", "kept", "g1").targets += 1
    fold.line("DEF", "dropped", "g1").targets += 1
    positions = {
        "kept": PlayerRef("kept", "WR", "WR", "K", "PHI", 1),
        "dropped": PlayerRef("dropped", "WR", "WR", "D", "PHI", 2),
    }
    totals = totals_from_fold(fold, positions=positions, resolved={"kept"})
    assert totals.per_game[("DEF", "WR", "g1")].targets == 1


def test_a_resolved_player_with_no_roster_row_is_still_dropped():
    """Belt and braces on the join: `resolved` and `positions` are two
    different maps, and a player in one but not the other cannot be attributed
    to a position at all."""
    fold = PbpFold()
    fold.line("DEF", "ghost", "g1").targets += 1
    totals = totals_from_fold(fold, positions={}, resolved={"ghost"})
    assert totals.per_game == {}

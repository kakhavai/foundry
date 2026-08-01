"""The rank-divergence guard, both arms.

The spec's named failure mode: "Defenses that build leads face pass-heavy
opponents in the fourth quarter, so a strong defense accumulates inflated
per-game WR and TE allowance while its per-opportunity allowance stays elite --
the raw rating then reads as a soft matchup precisely for the teams that are
hardest to score against."

Every field is populated and every value is plausible, so a null check cannot
see it. The catch is a comparison **across the league**: rank the 32 defenses
on each basis and flag any whose two ranks differ by more than eight places.

**A guard whose two arms look alike needs a fixture per arm.** A suite with
only the diverging fixture passes when the guard flags everything, and a suite
with only the agreeing one passes when it flags nothing. Both are here, and
both go end to end through the capture rather than calling `divergent_teams`
with hand-built dicts -- a unit test of the comparison cannot catch a caller
that ranks the wrong two fields.
"""

import pytest

from defense_vs_position.ratings import (
    RANK_DIVERGENCE_THRESHOLD,
    average_ranks,
    divergent_teams,
)

from . import season
from .conftest import SpyLake, run_capture

SIGNAL_TYPE = "defense_positional_allowance"
SKEWED = "BAL"


def rows_by_split(envelope, position: str, scoring_format: str) -> dict[str, dict]:
    return {
        row["team_id"]: row
        for row in envelope.signals
        if row["position"] == position and row["scoring_format"] == scoring_format
    }


# --------------------------------------------------------------------------
# Arm 1: the ranks agree
# --------------------------------------------------------------------------


async def test_a_league_whose_two_bases_agree_flags_nobody(upstreams):
    """Every defense faces the same volume, so volume cannot move a rank.

    If this arm is missing, a guard that flagged all 32 teams would still pass
    the divergence test below.
    """
    envelopes = await run_capture(SpyLake())
    envelope = envelopes[SIGNAL_TYPE]

    assert not [e for e in envelope.errors if e["reason"] == "rank_divergence"]
    assert not [r for r in envelope.signals if r["rank_divergence_flagged"]]


# --------------------------------------------------------------------------
# Arm 2: the ranks diverge, in the direction the spec names
# --------------------------------------------------------------------------


async def test_a_volume_skewed_defense_is_flagged(upstreams):
    """`BAL`'s opponents throw six times as often for a fifth of the yardage.

    Its per-game WR allowance inflates while its per-opportunity allowance
    stays elite -- and it is the ONLY team whose schedule was changed, so the
    assertion is that exactly it is flagged rather than that something was.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed(SKEWED)))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    flagged = {
        row["team_id"]
        for row in envelope.signals
        if row["position"] == "WR" and row["rank_divergence_flagged"]
    }
    assert SKEWED in flagged, "the guard missed the failure mode it exists for"

    entries = [e for e in envelope.errors if e["reason"] == "rank_divergence"]
    assert entries, "a flagged row must also reach coverage.errors"
    assert any(e["detail"].startswith(f"{SKEWED}/WR/all/") for e in entries)


async def test_the_flag_points_the_way_the_spec_says(upstreams):
    """Direction, not just magnitude.

    A guard that fired on any large gap would pass the test above while
    flagging the opposite fact. The spec is specific: the raw per-game rating
    reads SOFT (a high allowance, so a low rank number) for a defense that is
    elite per opportunity.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed(SKEWED)))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    rows = rows_by_split(envelope, "WR", "ppr")

    per_game = average_ranks(
        {t: r["fantasy_points_allowed_per_game"] for t, r in rows.items()}
    )
    per_opportunity = average_ranks(
        {t: r["fantasy_points_allowed_per_opportunity"] for t, r in rows.items()}
    )
    assert per_game[SKEWED] < per_opportunity[SKEWED], (
        "expected the skewed defense to look soft per game and elite per opportunity"
    )
    assert per_opportunity[SKEWED] - per_game[SKEWED] > RANK_DIVERGENCE_THRESHOLD


async def test_a_flagged_row_is_published_not_dropped(upstreams):
    """ "Flag", not "drop". The generator gets the row and the caveat."""
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed(SKEWED)))
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]
    row = rows_by_split(envelope, "WR", "ppr")[SKEWED]
    assert row["rank_divergence_flagged"] is True
    assert row["fantasy_points_allowed_per_game"] is not None
    assert row["fantasy_points_allowed_per_opportunity"] is not None


async def test_the_flag_count_reaches_the_metric(upstreams, monkeypatch):
    """The gauge is what alerts. Recorded with the real count, not a bool."""
    recorded: list[int] = []
    monkeypatch.setattr(
        "defense_vs_position.capture.metrics.rank_divergences", recorded.append
    )
    upstreams.set_pbp(season.pbp_document(drives=season.volume_skewed(SKEWED)))
    await run_capture(SpyLake())
    assert recorded and recorded[0] > 0


async def test_the_metric_is_recorded_at_zero_too(upstreams, monkeypatch):
    """An absent Prometheus series and a healthy one are indistinguishable, so
    "no divergence this week" and "the guard stopped running" must not look
    the same."""
    recorded: list[int] = []
    monkeypatch.setattr(
        "defense_vs_position.capture.metrics.rank_divergences", recorded.append
    )
    await run_capture(SpyLake())
    assert recorded == [0]


# --------------------------------------------------------------------------
# The comparison itself
# --------------------------------------------------------------------------


def test_the_threshold_is_the_specs_eight():
    """Not calibrated to today's distribution. `team-scheme` rejected a
    plausibility bound fitted to the data in front of it, because a threshold
    fitted to today's data is a filter on tomorrow's signal."""
    assert RANK_DIVERGENCE_THRESHOLD == 8.0


@pytest.mark.parametrize(
    ("gap", "flagged"),
    [
        pytest.param(8, False, id="exactly-eight-is-not-more-than-eight"),
        pytest.param(9, True, id="nine-is"),
    ],
)
def test_the_boundary_is_strictly_greater_than(gap, flagged):
    """`> 8`, not `>= 8`. The spec says "differ by more than eight places",
    and an off-by-one here changes what an operator is asked to review."""
    teams = [f"T{i:02d}" for i in range(32)]
    per_game = {team: float(32 - index) for index, team in enumerate(teams)}
    per_opportunity = dict(per_game)
    # Move exactly one team by `gap` places by swapping its value with the
    # team `gap` places away.
    a, b = teams[0], teams[gap]
    per_opportunity[a], per_opportunity[b] = per_opportunity[b], per_opportunity[a]

    result = divergent_teams(per_game, per_opportunity)
    assert (a in result) is flagged
    assert (b in result) is flagged


def test_ties_are_averaged_rather_than_broken_by_key_order():
    """Every defense that faced zero opportunities scores 0.0, and there are
    often several. Breaking that tie alphabetically manufactures a rank spread
    as wide as the tie group, which the guard would read as real
    disagreement."""
    values = {"AAA": 1.0, "BBB": 0.0, "CCC": 0.0, "DDD": 0.0}
    assert average_ranks(values) == {"AAA": 1.0, "BBB": 3.0, "CCC": 3.0, "DDD": 3.0}
    # And a whole league of ties diverges from itself by nothing.
    flat = dict.fromkeys(("AAA", "BBB", "CCC", "DDD"), 0.0)
    assert divergent_teams(flat, flat) == {}


def test_rank_one_is_the_largest_allowance():
    """Direction is part of the contract: the tests above read `per_game[x] <
    per_opportunity[x]` as "looks softer per game", which is only true if a
    low rank number means a high allowance."""
    assert average_ranks({"HIGH": 10.0, "LOW": 1.0}) == {"HIGH": 1.0, "LOW": 2.0}


def test_teams_present_in_only_one_basis_are_excluded_not_fatal():
    """**One incomparable team must not disable the guard for the league.**

    `per_game` and `per_opportunity` admit a team only where its rate is
    non-`None`, so a single team with games but zero opportunities used to
    make the key sets unequal and return `{}` for all 32 teams of that
    position and scoring format -- silently, with the divergence gauge
    recording a perfectly plausible zero.

    Reachable rather than theoretical: the fumble branch in `_fold_players`
    adds a game to `games` without incrementing `opportunities`, so a player
    whose only involvement in a game was a lost fumble produces exactly that
    line.

    The comparable teams are still ranked; the odd one out is dropped from
    both rankings together, so it cannot shift anyone else's rank either.
    """
    teams = [f"T{i:02d}" for i in range(32)]
    per_game = {team: float(32 - index) for index, team in enumerate(teams)}
    per_opportunity = dict(per_game)
    a, b = teams[0], teams[12]
    per_opportunity[a], per_opportunity[b] = per_opportunity[b], per_opportunity[a]

    both = divergent_teams(per_game, per_opportunity)
    assert {a, b} <= set(both), "the baseline population must flag the swap"

    # Now one extra team has a per-game rate and no per-opportunity rate.
    per_game["ODD"] = 99.0
    partial = divergent_teams(per_game, per_opportunity)
    assert {a, b} <= set(partial), (
        "one incomparable team disabled the guard for the whole split"
    )
    assert "ODD" not in partial


async def test_a_fumble_only_line_does_not_silence_the_split(upstreams):
    """The same thing end to end, through the fold that can actually produce
    it: a receiver whose only involvement in a game is a lost fumble."""
    ghost = season.Play(
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
            "fumble_lost": 1,
            "fumbled_1_player_id": season.gsis_id("ARI", "WR"),
            "fumbled_1_team": "ARI",
        },
    ).to_row()
    upstreams.set_pbp(
        season.pbp_document(drives=season.volume_skewed(SKEWED), extra_rows=[ghost])
    )
    envelope = (await run_capture(SpyLake()))[SIGNAL_TYPE]

    flagged = {
        row["team_id"]
        for row in envelope.signals
        if row["position"] == "WR" and row["rank_divergence_flagged"]
    }
    assert SKEWED in flagged, (
        "a fumble-only line silenced the guard for the whole WR split"
    )


def test_two_disjoint_populations_still_yield_nothing():
    """No overlap means nothing is comparable, which is genuinely `{}`."""
    assert divergent_teams({"A": 1.0}, {"B": 1.0}) == {}
    assert divergent_teams({}, {}) == {}

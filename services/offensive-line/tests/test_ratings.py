"""The arithmetic: line yards, the opponent adjustment, hashes, continuity.

Pure-function tests where they can be, and end-to-end through the real capture
where the claim is about published rows. The fixture is built as
`f(offence) + g(defence)` precisely so the adjustment tests below can fail —
see `tests/season.py`.
"""

import hashlib

import pytest

from offensive_line import ratings
from offensive_line.ratings import (
    LINE_YARDS_CAP,
    LINE_YARDS_FULL_MAX,
    LINE_YARDS_HALF_MAX,
    STARTER_POSITIONS,
    StarterSlot,
    continuity_games,
    line_yards,
    lineup_hash,
    opponent_strengths,
)

from . import season as season_module
from .conftest import Feeds, SpyLake, run_capture, units

# --------------------------------------------------------------------------
# The Football Outsiders weighting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("yards", "credited"),
    [
        (-3.0, -3.6),  # 120% charged to the line behind the line of scrimmage
        (0.0, 0.0),
        (2.0, 2.0),  # full credit through four
        (4.0, 4.0),
        (7.0, 5.5),  # half credit from five to ten
        (10.0, 7.0),
        (40.0, 7.0),  # nothing past ten: the open field is the back's
    ],
)
def test_line_yards_follows_the_published_bands(yards, credited):
    assert line_yards(yards) == pytest.approx(credited)


def test_the_cap_is_derived_from_the_bands_rather_than_written_down():
    """A change to the weighting cannot leave the cap behind."""
    assert LINE_YARDS_CAP == line_yards(LINE_YARDS_HALF_MAX)
    assert LINE_YARDS_CAP == line_yards(LINE_YARDS_HALF_MAX + 100.0)
    assert line_yards(LINE_YARDS_FULL_MAX) == LINE_YARDS_FULL_MAX


# --------------------------------------------------------------------------
# The opponent adjustment
# --------------------------------------------------------------------------


def test_strength_is_leave_one_out_and_relative_to_the_league():
    """The game being adjusted must not appear on both sides of its own
    adjustment: a line that shut out a front would otherwise be told that
    front is weak partly *because* it was shut out."""
    production = {("X", "g1"): 0.4, ("X", "g2"): 0.2, ("Y", "g1"): 0.3}
    strengths = opponent_strengths(production)
    league_mean = (0.4 + 0.2 + 0.3) / 3
    # X's strength in g1 is built from g2 alone.
    assert strengths[("X", "g1")] == pytest.approx(0.2 / league_mean)
    assert strengths[("X", "g2")] == pytest.approx(0.4 / league_mean)
    # Y has one game, so it falls back to that game.
    assert strengths[("Y", "g1")] == pytest.approx(0.3 / league_mean)


def test_a_league_that_produced_nothing_is_all_average():
    assert opponent_strengths({("X", "g1"): 0.0}) == {("X", "g1"): 1.0}


def test_no_production_at_all_is_an_empty_map_not_a_crash():
    assert opponent_strengths({}) == {}


async def test_the_adjusted_column_keeps_real_spread():
    """**The constant-valued bug, made unshippable.**

    `defense-vs-position` published an adjusted column that was the league
    mean for all 32 teams while the raw values spanned 4x, because it adjusted
    a unit by its own leave-one-out mean of the quantity being rated — and the
    mean of a unit's leave-one-out means is exactly its full mean. Every field
    was populated and plausible; the tell was zero variance.
    """
    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    adjusted = [row["pressure_rate_allowed_adj_observed"] for row in rows.values()]
    assert len(set(adjusted)) > 1
    spread = max(adjusted) - min(adjusted)
    assert spread > 0.01, f"the adjusted column collapsed to a constant: {adjusted}"


async def test_the_adjustment_tracks_the_line_term_not_the_front_term():
    """A *correct* adjustment removes the opponent's contribution and leaves
    the line's own. This is the arm the degenerate fixture cannot have: if
    production varied by the offence alone, a correct adjustment would remove
    100% of the variance and collapse to the mean — bit-for-bit identical to
    the bug above.

    Restricted to the teams that never changed personnel, and that is not a
    convenience. A churn team's window rate carries a real third term — two
    weeks with a worse left tackle — so ranking it by `LINE_WEAKNESS` alone
    would be asserting the adjustment removes something it should keep.
    """
    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    stable = season_module.TEAMS[: season_module.CHURN_FROM]
    ranked = sorted(
        stable, key=lambda team: rows[team]["pressure_rate_allowed_adj_observed"]
    )
    weakness = [season_module.LINE_WEAKNESS[team] for team in ranked]
    assert weakness == sorted(weakness), (
        "the adjusted ranking should follow the lines' own weakness term, "
        f"not the schedule: {ranked}"
    )


async def test_the_opponent_index_is_centred_on_one():
    """`1.0` is average — the unit the field is documented in. A collector
    that normalised differently would still produce spread and would silently
    break the differential against defensive-front."""
    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    indices = [row["opponent_pressure_strength_index"] for row in rows.values()]
    assert sum(indices) / len(indices) == pytest.approx(1.0, abs=0.05)


def test_a_collapsed_opponent_index_leaves_the_raw_rate_alone():
    """Below `MIN_OPPONENT_STRENGTH` the index is not trusted to divide by: a
    slate of fronts that generated essentially nothing would otherwise turn a
    small raw rate into an enormous adjusted one."""
    assert ratings._adjust(0.3, ratings.MIN_OPPONENT_STRENGTH / 2) == 0.3
    assert ratings._adjust(0.3, 1.0) == 0.3
    assert ratings._adjust(0.3, 2.0) == pytest.approx(0.15)
    assert ratings._adjust(None, 2.0) is None


# --------------------------------------------------------------------------
# The hash and the streak
# --------------------------------------------------------------------------


def _slots(*ids: str) -> list[StarterSlot]:
    return [
        StarterSlot(position=position, gsis_id=gsis_id)
        for position, gsis_id in zip(STARTER_POSITIONS, ids, strict=False)
    ]


def test_the_hash_needs_all_five_slots():
    """Four ids would hash to a stable value no consumer could tell from a
    real five — which is why the spec puts a short team in coverage.missing."""
    assert lineup_hash(_slots("a", "b", "c", "d")) is None
    assert lineup_hash(_slots("a", "b", "c", "d", "e")) is not None


def test_the_hash_is_position_ordered_not_a_set():
    """Two lines with the same five men in different slots are different
    lines. A set hash would call a tackle/guard swap continuity."""
    straight = lineup_hash(_slots("a", "b", "c", "d", "e"))
    swapped = lineup_hash(_slots("b", "a", "c", "d", "e"))
    assert straight != swapped


def test_the_hash_is_stable_across_processes():
    """`hashlib`, never `hash()`: Python salts `hash()` on `str` per process,
    so a salted hash would differ between two pods and between one pod's
    restarts, and every week would look like a personnel change. Pinned
    against the digest rather than against itself, because comparing a value
    to another call in the same process cannot see the salt."""
    expected = hashlib.sha256(b"a|b|c|d|e").hexdigest()[:16]
    assert lineup_hash(_slots("a", "b", "c", "d", "e")) == expected
    assert len(expected) == 16


@pytest.mark.parametrize(
    ("hashes", "streak"),
    [
        ([], 0),
        (["x"], 0),
        (["x", "x"], 1),
        (["x", "x", "x"], 2),
        (["x", "y"], 0),  # changed between the last two games
        (["y", "x", "x"], 1),
        ([None, "x", "x"], 1),
        (["x", None, "x"], 0),  # an unreadable game stops the walk
        (["x", "x", None], 0),  # the current game itself is unreadable
    ],
)
def test_continuity_counts_consecutive_prior_games(hashes, streak):
    assert continuity_games(hashes) == streak


async def test_a_stable_line_reports_a_streak_and_a_changed_one_reports_zero():
    """Both arms, from the real capture rather than from the pure function.

    Half the fixture's teams bench their left tackle for two weeks and restore
    him in week 5; the other half never change. A collector that ordered its
    games by document order rather than by week would report the two the wrong
    way round, and nothing about the row would look malformed.
    """
    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    stable = rows[season_module.TEAMS[0]]
    churned = rows[season_module.TEAMS[season_module.CHURN_FROM]]

    assert stable["lineup_changed"] is False
    assert stable["continuity_games"] == season_module.WEEKS - 1

    assert churned["lineup_changed"] is True
    assert churned["continuity_games"] == 0


async def test_the_hash_is_decided_by_snaps_and_not_by_the_depth_chart():
    """The spec's own warning, made into a test.

    The fixture's preseason depth chart names the swing man as the starting
    left tackle for **every** team, and the weekly charts name the incumbent.
    In weeks 3 and 4 the churn teams actually play the swing man. If the hash
    were read off the published chart, the churn teams' week-5 hash would
    equal their week-4 hash — the chart never changed — and `lineup_changed`
    would be false. It is the snap counts that make it true.
    """
    rows = units(await run_capture(Feeds(), lake=SpyLake()))
    for index, team in enumerate(season_module.TEAMS):
        expected = index >= season_module.CHURN_FROM
        assert rows[team]["lineup_changed"] is expected, team

"""Coverage: the floor AND the `present` predicate.

`coverage.expected` must never derive from what a fetch returned, and a
defense must count present only when **every** declared split carries a real
sample. Both halves are load-bearing and they fail differently:

* a fetch-derived floor reports a four-team truncation as a perfect week;
* a hollowed-out `present` predicate reports 32 defenses of empty rows as a
  perfect week.

A mutation set that attacks only the first scores well and still misses the
second, which is why `ground_only` exists as a fixture.
"""

import pytest

from defense_vs_position.capture import EXPECTED_FLOOR, NFL_TEAMS, SIGNAL_TYPES
from defense_vs_position.ratings import declared_splits
from defense_vs_position.scoring import ALIGNMENTS, POSITIONS, SCORING_FORMATS

from . import season
from .conftest import SpyLake, run_capture

SIGNAL_TYPE = "defense_positional_allowance"


async def test_a_truncated_upstream_does_not_report_full_coverage(upstreams):
    """The failure the floor exists for: a play-by-play describing four teams
    must not yield `expected: 4, present: 4`, ratio 1.0."""
    upstreams.set_pbp(season.pbp_document(teams=season.TEAMS[:4]))
    envelopes = await run_capture(SpyLake())

    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.coverage.expected == NFL_TEAMS
    assert envelope.coverage.present == 4
    assert envelope.coverage.ratio == pytest.approx(4 / 32)
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


async def test_a_defense_with_empty_splits_is_not_present(upstreams):
    """The OTHER half. `BUF`'s opponents run the ball and nothing else, so its
    QB, WR, TE and K rows exist with `games_sampled == 0`.

    Those rows are published -- 576 either way -- and the defense must still
    not count as covered. A `present` predicate that only checked "a row
    exists" reports this as a complete league.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.ground_only("BUF")))
    envelopes = await run_capture(SpyLake())
    envelope = envelopes[SIGNAL_TYPE]

    assert len(envelope.signals) == len(season.TEAMS) * declared_splits(), (
        "the row set must be unchanged: this is about presence, not rows"
    )
    assert envelope.coverage.present == NFL_TEAMS - 1
    assert envelope.coverage.missing == ["BUF"]
    incomplete = [e for e in envelope.errors if e["reason"] == "incomplete_splits"]
    assert len(incomplete) == 1
    # RB x 3 formats, plus DST x 3 -- BUF's own offense still ran plays, so
    # its DST split is sampled. QB, WR, TE and K are the four it never faced.
    assert incomplete[0]["detail"].startswith("BUF: 6/18")


async def test_the_empty_splits_are_null_rather_than_zero(upstreams):
    """A rate with no denominator is unknown, not zero.

    `0.0` is a real observation a generator would average in and rate as an
    elite matchup; `None` is the absence of one.
    """
    upstreams.set_pbp(season.pbp_document(drives=season.ground_only("BUF")))
    envelopes = await run_capture(SpyLake())
    rows = {
        (r["team_id"], r["position"], r["scoring_format"]): r
        for r in envelopes[SIGNAL_TYPE].signals
    }
    empty = rows[("BUF", "WR", "ppr")]
    assert empty["games_sampled"] == 0
    assert empty["opportunities_defended"] == 0
    assert empty["fantasy_points_allowed_per_game"] is None
    assert empty["fantasy_points_allowed_per_opportunity"] is None
    assert empty["fantasy_points_allowed_per_game_adj"] is None
    # And the RB split it DID face is populated, so this is a hole rather
    # than a dead team.
    assert rows[("BUF", "RB", "ppr")]["games_sampled"] == 2


async def test_an_empty_upstream_reports_zero_not_one(upstreams):
    """`Coverage.ratio` returns 1.0 when `expected` is 0 -- correct for a bye
    week, catastrophic for a pass that captured nothing."""
    upstreams.set_pbp(season.pbp_document(weeks=0))
    envelopes = await run_capture(SpyLake())
    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.signals == []
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == NFL_TEAMS
    assert envelope.coverage.ratio == 0.0


async def test_expansion_past_the_floor_still_reports_honestly(upstreams):
    """The floor must not CAP a genuine count, only raise a short one.

    Driven with a 34-team league, and asserted against the observed count
    rather than against 32 -- so replacing `max(observed, floor)` with the
    floor itself fails here.
    """
    expanded = (*season.TEAMS, "SAT", "LON")
    upstreams.set_players(
        season.players_document(
            extra=[
                {
                    "gsis_id": f"00-99{index}0000",
                    "display_name": f"{team} QB",
                    "position": "QB",
                    "jersey_number": "1",
                    "latest_team": team,
                }
                for index, team in enumerate(("SAT", "LON"))
            ]
        )
    )
    upstreams.set_pbp(season.pbp_document(teams=expanded, weeks=1))
    envelope = (await run_capture(SpyLake(), week=1))[SIGNAL_TYPE]
    assert envelope.coverage.expected == len(expanded)
    assert envelope.coverage.expected > NFL_TEAMS


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())
    assert EXPECTED_FLOOR["defense_positional_allowance"] == 32


def test_the_declared_split_count_is_derived_not_written_down():
    """`declared_splits` is what the `present` predicate compares against, so
    it must follow the dimension tuples rather than a literal. Unlocking an
    alignment or adding a scoring format has to move it automatically."""
    assert declared_splits() == len(POSITIONS) * len(ALIGNMENTS) * len(SCORING_FORMATS)
    assert declared_splits() == 18

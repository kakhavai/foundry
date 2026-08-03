"""The wire format, per feed. Each adapter is the only module that knows one.

The claims here are about parsing and narrowing — the layer where a wrong
answer is populated, plausible and silent. Every fixture body is real CSV
through `csv.writer`, gzipped where the release asset is gzipped.
"""

import gzip

import httpx
import pytest
import respx
from collector_core.streaming import UpstreamSchemaError, UpstreamTruncated

from offensive_line.adapters import depth as depth_adapter
from offensive_line.adapters import injuries as injuries_adapter
from offensive_line.adapters import participation as participation_adapter
from offensive_line.adapters import pbp as pbp_adapter
from offensive_line.adapters import players as players_adapter
from offensive_line.adapters import snaps as snaps_adapter
from offensive_line.lineups import derive_lineups

from . import season as season_module
from .conftest import SEASON, WEEK, Feeds


async def _fetch(name: str, feeds: Feeds, call):
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            return await call(client)


# --------------------------------------------------------------------------
# play-by-play
# --------------------------------------------------------------------------


async def test_pbp_indexes_dropbacks_and_folds_runs():
    fold = await _fetch(
        "pbp", Feeds(), lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c)
    )
    games = len(season_module.games())
    assert len(fold.dropbacks) == games * 2 * season_module.SNAPS_PER_GAME
    assert fold.weeks == set(range(1, season_module.WEEKS + 1))
    # Both directions of every game, so the adjustment cannot be fit against a
    # different schedule than the rates were computed over.
    assert len(fold.opponents) == games * 2


async def test_pbp_carries_the_week_calendar_depth_charts_lacks():
    """`depth_charts` has no season or week column at all, so the only way to
    ask for "the chart current in week 5" is a date, and this is the only feed
    that has one."""
    fold = await _fetch(
        "pbp", Feeds(), lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c)
    )
    assert fold.week_dates == {
        week: season_module.game_date(week)
        for week in range(1, season_module.WEEKS + 1)
    }


async def test_pbp_excludes_the_postseason_and_the_weeks_after_the_window():
    built = season_module.build_season()
    feeds = Feeds(bodies={"pbp": season_module.pbp_document(built, season_type="POST")})
    fold = await _fetch(
        "pbp", feeds, lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c)
    )
    assert fold.dropbacks == {}

    fold = await _fetch(
        "pbp", Feeds(), lambda c: pbp_adapter.fetch_pbp(SEASON, 2, client=c)
    )
    assert fold.weeks == {1, 2}


async def test_a_truncated_gzip_body_raises_rather_than_returning_half():
    """The failure that is otherwise *plausible*. A body cut in half yields a
    short document with a fragment for a last row and no exception at all
    without the trailer check — and a short season reads as a quiet week."""
    whole = season_module.pbp_document(season_module.build_season())
    feeds = Feeds(bodies={"pbp": whole[: len(whole) // 2]})
    with pytest.raises(UpstreamTruncated):
        await _fetch(
            "pbp", feeds, lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c)
        )


async def test_a_renamed_column_fails_the_header_check():
    """Schema drift has to fail immediately rather than after a million rows
    have been mapped to nulls — an append-only lake is never rewritten."""
    body = gzip.decompress(
        season_module.pbp_document(season_module.build_season())
    ).decode()
    broken = gzip.compress(body.replace("qb_dropback", "dropback_qb", 1).encode())
    with pytest.raises(UpstreamSchemaError):
        await _fetch(
            "pbp",
            Feeds(bodies={"pbp": broken}),
            lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
        )


# --------------------------------------------------------------------------
# participation
# --------------------------------------------------------------------------


async def test_participation_reads_pressure_by_equality_not_truthiness():
    """This column's other value is the STRING `"FALSE"`, which is truthy. A
    collector reading it for truthiness reports a 100% pressure rate."""
    snaps = await _fetch(
        "participation",
        Feeds(),
        lambda c: participation_adapter.fetch_block_snaps(SEASON, client=c),
    )
    pressures = sum(1 for snap in snaps.values() if snap.was_pressure)
    assert 0 < pressures < len(snaps)


async def test_participation_drops_every_row_with_no_pass_rushers():
    """Every run, kick and punt — about half the document — filtered as it is
    parsed rather than materialised and narrowed afterwards.

    The count includes the penalty-nullified `no_play` snaps, which ARE
    charted pass rushes: this adapter's job is to report every charted rush,
    and deciding which of them is a scrimmage dropback belongs to the join.
    """
    snaps = await _fetch(
        "participation",
        Feeds(),
        lambda c: participation_adapter.fetch_block_snaps(SEASON, client=c),
    )
    games = len(season_module.games())
    per_team_game = season_module.SNAPS_PER_GAME + season_module.NO_PLAYS_PER_GAME
    assert len(snaps) == games * 2 * per_team_game


async def test_participation_does_not_read_the_player_lists():
    """Pressure is attributed to the unit, so the eleven-id columns are
    projected away. The fixture populates them precisely so a collector that
    started distributing a pressure across five blockers would have data to do
    it with — and `BlockSnap` has nowhere to put it."""
    snaps = await _fetch(
        "participation",
        Feeds(),
        lambda c: participation_adapter.fetch_block_snaps(SEASON, client=c),
    )
    assert set(next(iter(snaps.values())).__slots__) == {
        "was_pressure",
        "time_to_throw",
    }


async def test_release_time_is_absent_on_the_dropbacks_that_have_none():
    """A sack, scramble or throwaway has no release. 42.8% populated on the
    real feed, and null rather than zero here — a zero would drag every
    `mean_time_to_throw` toward the floor."""
    snaps = await _fetch(
        "participation",
        Feeds(),
        lambda c: participation_adapter.fetch_block_snaps(SEASON, client=c),
    )
    charted = [snap for snap in snaps.values() if snap.time_to_throw is not None]
    assert 0 < len(charted) < len(snaps)
    assert all(snap.time_to_throw > 0 for snap in charted)


# --------------------------------------------------------------------------
# snap counts
# --------------------------------------------------------------------------


async def test_snaps_narrow_to_linemen_and_derive_the_team_total():
    fold = await _fetch(
        "snaps", Feeds(), lambda c: snaps_adapter.fetch_snaps(SEASON, WEEK, client=c)
    )
    assert {entry.position for entry in fold.line} <= snaps_adapter.LINE_POSITIONS
    assert all(
        total == season_module.TEAM_SNAPS_PER_GAME
        for total in fold.team_offense_snaps.values()
    )
    # The quarterback sets the total and is not a lineman.
    assert not any(entry.pfr_id == season_module.skill_id("AAA") for entry in fold.line)


async def test_snaps_stop_at_the_requested_week():
    fold = await _fetch(
        "snaps", Feeds(), lambda c: snaps_adapter.fetch_snaps(SEASON, 2, client=c)
    )
    assert {entry.week for entry in fold.line} == {1, 2}


# --------------------------------------------------------------------------
# depth charts
# --------------------------------------------------------------------------


async def test_depth_charts_label_from_the_snapshot_current_for_that_week():
    """**The bug this lookup exists to prevent.** The fixture's preseason
    snapshot names the swing man as every team's starting left tackle; the
    weekly ones name the incumbent. An adapter that took the newest snapshot,
    or the first, would label every week from the wrong chart — and mislabelled
    slots reorder `lineup_hash` and report churn on lines that never changed.
    """
    charts = await _fetch(
        "depth", Feeds(), lambda c: depth_adapter.fetch_depth_charts(SEASON, client=c)
    )
    team = season_module.TEAMS[0]
    incumbent = season_module.line_id(team, 0)
    swing = season_module.line_id(team, season_module.SWING_SLOT)

    preseason = charts.labels_at(season_module.PRESEASON_CHART_DATE)
    assert preseason[(team, swing)] == "LT"
    assert (team, incumbent) not in preseason

    in_season = charts.labels_at(season_module.game_date(3))
    assert in_season[(team, incumbent)] == "LT"
    # The swing man is still labelled — he is a listed backup at the same
    # slot, and a rank-1-only reader would leave a week he actually played
    # with four labelled slots and no hash at all.
    assert in_season[(team, swing)] == "LT"


async def test_a_game_before_every_snapshot_falls_back_to_the_earliest():
    charts = await _fetch(
        "depth", Feeds(), lambda c: depth_adapter.fetch_depth_charts(SEASON, client=c)
    )
    assert charts.labels_at("2020-01-01") == charts.labels_at(
        season_module.PRESEASON_CHART_DATE
    )
    assert charts.labels_at(None) == charts.labels_at(charts.dates[-1])


async def test_depth_charts_keep_only_the_five_line_slots():
    charts = await _fetch(
        "depth", Feeds(), lambda c: depth_adapter.fetch_depth_charts(SEASON, client=c)
    )
    labels = charts.labels_at(season_module.game_date(1))
    assert set(labels.values()) <= depth_adapter.LINE_SLOTS


# --------------------------------------------------------------------------
# players — the crosswalk
# --------------------------------------------------------------------------


async def test_players_bridges_the_two_id_vocabularies():
    """`snap_counts` is keyed by `pfr_player_id` and `depth_charts` by
    `gsis_id`, and nothing joins them directly. This is the bridge, and on the
    real 2025 season it carries 99.6% of the line's snap rows."""
    roster = await _fetch(
        "players", Feeds(), lambda c: players_adapter.fetch_line_players(client=c)
    )
    team, slot = season_module.TEAMS[0], 0
    assert roster.gsis_for_pfr[season_module.pfr_id(team, slot)] == (
        season_module.line_id(team, slot)
    )
    # The quarterback is in the document and must not be in the crosswalk: a
    # collector that skipped the position-group narrowing would publish him as
    # a left tackle the moment a depth chart disagreed with a snap count.
    assert season_module.skill_id(team) not in roster.gsis_for_pfr


async def test_players_reports_injured_reserve():
    """The one value the weekly injury report cannot produce — verified live:
    its `report_status` column carries only Out/Questionable/Doubtful/blank."""
    roster = await _fetch(
        "players", Feeds(), lambda c: players_adapter.fetch_line_players(client=c)
    )
    hurt = season_module.line_id(season_module.IR_TEAM, season_module.IR_SLOT)
    assert roster.by_gsis[hurt].on_ir is True
    assert not roster.by_gsis[season_module.line_id("AAA", 0)].on_ir


async def test_a_crosswalk_gap_costs_that_player_not_the_feed():
    """A row with no `pfr_id` is a real shape — 521 of 4,125 linemen on the
    live feed. It must cost that man's snaps, not the whole roster."""
    missing = frozenset({("AAA", 0)})
    feeds = Feeds(
        bodies={"players": season_module.players_document(drop_crosswalk=missing)}
    )
    roster = await _fetch(
        "players", feeds, lambda c: players_adapter.fetch_line_players(client=c)
    )
    assert season_module.pfr_id("AAA", 0) not in roster.gsis_for_pfr
    assert season_module.line_id("AAA", 0) in roster.by_gsis
    assert len(roster.gsis_for_pfr) > 1


# --------------------------------------------------------------------------
# injuries
# --------------------------------------------------------------------------


async def test_injuries_read_the_upcoming_week_only():
    """`week` is the last week sampled, so the upcoming week is `week + 1`.
    The fixture carries a row for the wrong week and a postseason row for the
    right one; both must be filtered."""
    availability = await _fetch(
        "injuries",
        Feeds(),
        lambda c: injuries_adapter.fetch_availability(SEASON, WEEK, client=c),
    )
    assert availability[season_module.line_id("AAA", 0)] == "out"
    assert availability[season_module.line_id("BBB", 2)] == "questionable"
    assert availability[season_module.line_id("EEE", 1)] == "doubtful"
    # A blank report line is a practice-report entry, not an absence.
    assert availability[season_module.line_id("FFF", 4)] == "active"
    assert season_module.line_id("GGG", 0) not in availability
    assert season_module.line_id("HHH", 4) not in availability


async def test_the_last_week_of_a_season_has_no_upcoming_week():
    """Correct rather than a gap: there is no game to be available for, and
    every starter then reads `active` by default rather than being falsely
    marked out."""
    availability = await _fetch(
        "injuries",
        Feeds(),
        lambda c: injuries_adapter.fetch_availability(SEASON, 18, client=c),
    )
    assert availability == {}


# --------------------------------------------------------------------------
# the join the adapters exist to make possible
# --------------------------------------------------------------------------


async def test_the_lineup_is_decided_by_snaps_within_the_depth_charts_labels():
    """The two questions, kept apart. `depth_charts` says the swing man is a
    left tackle; `snap_counts` says who actually took the snaps. In weeks 3
    and 4 that is the swing man, in every other week the incumbent, and the
    label is identical throughout."""
    feeds = Feeds()
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            fold = await pbp_adapter.fetch_pbp(SEASON, WEEK, client=client)
            snap_fold = await snaps_adapter.fetch_snaps(SEASON, WEEK, client=client)
            charts = await depth_adapter.fetch_depth_charts(SEASON, client=client)
            roster = await players_adapter.fetch_line_players(client=client)

    lineups = derive_lineups(snap_fold, roster, charts, fold.week_dates)
    team = season_module.TEAMS[season_module.CHURN_FROM]
    by_week = {
        fold.game_week[game]: {slot.position: slot.gsis_id for slot in slots}
        for (found, game), slots in lineups.items()
        if found == team
    }
    for week in range(1, season_module.WEEKS + 1):
        expected = season_module.line_id(
            team, season_module.starting_slot(team, week, 0)
        )
        assert by_week[week]["LT"] == expected, week

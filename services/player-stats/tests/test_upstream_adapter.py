"""The nflverse box-score adapter: the wire format, and what it refuses.

Exercised through the real streaming path (`respx` serving a CSV body) rather
than by handing `parse_box_rows` a list, because the streaming reader is where
the memory rules live and a list-based test would never touch it.
"""

import httpx
import pytest
import respx
from collector_core.streaming import UpstreamSchemaError

from player_stats.adapters.upstream import (
    REQUIRED_COLUMNS,
    SCOPE_POSITIONS,
    UpstreamRowError,
    _box_row,
    _int,
    fetch_box_rows,
    parse_box_rows,
    source_ref,
    stats_url,
)

from .conftest import NOW, SEASON, WEEK, feed_csv, feed_row


async def _fetch(rows, *, season=SEASON, week=WEEK):
    url = stats_url(season)
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, text=feed_csv(rows)))
        async with httpx.AsyncClient() as client:
            return await fetch_box_rows(season, week, client=client, now=NOW)


async def test_a_row_maps_onto_the_counting_stat_blocks():
    rows, errors = await _fetch(
        [
            feed_row(
                position="QB",
                attempts=30,
                completions=20,
                passing_yards=250,
                passing_tds=2,
                passing_interceptions=1,
                sacks_suffered=3,
                passing_air_yards=300,
            )
        ]
    )
    assert errors == []
    assert len(rows) == 1
    assert rows[0].passing == {
        "attempts": 30,
        "completions": 20,
        "yards": 250,
        "touchdowns": 2,
        "interceptions": 1,
        "sacks_taken": 3,
        "air_yards": 300,
    }


async def test_two_point_conversions_sum_across_all_three_columns():
    """Three upstream columns, one emitted field — a sum, not a pick."""
    rows, _ = await _fetch(
        [
            feed_row(
                passing_2pt_conversions=1,
                rushing_2pt_conversions=2,
                receiving_2pt_conversions=4,
            )
        ]
    )
    assert len(rows) == 1
    assert rows[0].misc["two_point_conversions"] == 7


async def test_another_week_is_dropped_during_the_parse():
    """Filtered as parsed, not after — the feed carries the whole season."""
    rows, errors = await _fetch(
        [
            feed_row(player_id="00-0000001", week=WEEK),
            feed_row(player_id="00-0000002", week=WEEK + 1),
            feed_row(player_id="00-0000003", week=WEEK + 9),
        ]
    )
    assert errors == []
    assert [row.upstream_player_id for row in rows] == ["00-0000001"]


async def test_another_season_is_dropped():
    rows, _ = await _fetch([feed_row(season=SEASON - 1)])
    assert rows == []


async def test_a_defensive_row_with_no_offensive_involvement_is_dropped():
    rows, _ = await _fetch([feed_row(position="CB")])
    assert rows == []


async def test_a_defensive_row_that_touched_the_ball_is_kept():
    """A defensive lineman who caught a touchdown is a real box score."""
    rows, _ = await _fetch([feed_row(position="DE", targets=1, receptions=1)])
    assert len(rows) == 1
    assert rows[0].position == "DE"


async def test_a_kicker_with_no_offensive_involvement_is_kept():
    """K is a scope position and takes no offensive snap — an offence-only
    involvement test would silently lose every kicker in the league."""
    rows, _ = await _fetch([feed_row(position="K", fg_att=3, pat_att=2)])
    assert len(rows) == 1


async def test_every_scope_position_survives_a_zero_stat_line():
    """A dressed WR4 with no targets is still a row this collector is owed."""
    rows, _ = await _fetch(
        [
            feed_row(player_id=f"00-000000{index}", position=position)
            for index, position in enumerate(sorted(SCOPE_POSITIONS))
        ]
    )
    assert len(rows) == len(SCOPE_POSITIONS)
    assert {row.position for row in rows} == set(SCOPE_POSITIONS)


async def test_a_renamed_column_fails_the_whole_fetch_loudly():
    """Schema drift must fail with `reason=malformed`, never map nulls into an
    append-only lake. `UpstreamSchemaError` subclasses ValueError, which is
    what `CollectorMetrics.reason_for` classifies as `malformed`."""
    document = feed_csv([feed_row()]).replace("receiving_yards", "rec_yards", 1)
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(200, text=document)
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(UpstreamSchemaError):
                await fetch_box_rows(SEASON, WEEK, client=client, now=NOW)


async def test_an_http_error_propagates_rather_than_returning_nothing():
    """An empty list would be recorded as a successful capture of nothing."""
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_box_rows(SEASON, WEEK, client=client, now=NOW)


async def test_a_non_numeric_cell_costs_one_row_not_the_pass():
    rows, errors = await _fetch(
        [
            feed_row(player_id="00-0000001", receiving_yards="forty"),
            feed_row(player_id="00-0000002", receiving_yards=40),
        ]
    )
    assert [row.upstream_player_id for row in rows] == ["00-0000002"]
    assert len(errors) == 1
    assert errors[0]["reason"] == "malformed_row"
    assert "00-0000001" in errors[0]["detail"]


def test_a_blank_cell_is_zero_not_an_error():
    """The feed leaves a cell empty for a stat a position cannot record."""
    assert _int({"targets": ""}, "targets") == 0
    assert _int({}, "targets") == 0


def test_an_integral_column_written_as_a_float_still_parses():
    assert _int({"attempts": "13.0"}, "attempts") == 13


def test_a_row_without_a_game_id_is_refused():
    with pytest.raises(UpstreamRowError):
        _box_row(feed_row(game_id=""))


def test_a_row_without_a_player_id_is_refused():
    with pytest.raises(UpstreamRowError):
        _box_row(feed_row(player_id=""))


def test_parse_box_rows_agrees_with_the_streaming_path():
    """The convenience parser is the same filter, so a test using it is not
    testing something the production path does not do."""
    rows, errors = parse_box_rows(
        [feed_row(position="WR"), feed_row(position="CB", week=WEEK)], SEASON, WEEK
    )
    assert errors == []
    assert [row.position for row in rows] == ["WR"]


def test_source_ref_names_the_season_asset():
    assert source_ref(SEASON, WEEK) == stats_url(SEASON)
    assert str(SEASON) in source_ref(SEASON, WEEK)


def test_the_required_column_set_is_not_empty():
    """`stream_csv_dicts` validates nothing when handed an empty set, so an
    empty `REQUIRED_COLUMNS` would make every drift assertion vacuous."""
    assert len(REQUIRED_COLUMNS) >= 30


@pytest.mark.parametrize("column", sorted(REQUIRED_COLUMNS))
async def test_renaming_any_single_required_column_fails_the_fetch(column):
    """Every declared column is genuinely load-bearing: drop one and the fetch
    must refuse rather than map a null into the lake."""
    document = feed_csv([feed_row()])
    header, _, body = document.partition("\n")
    columns = [f"{c}_renamed" if c == column else c for c in header.split(",")]
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(200, text=f"{','.join(columns)}\n{body}")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(UpstreamSchemaError):
                await fetch_box_rows(SEASON, WEEK, client=client, now=NOW)

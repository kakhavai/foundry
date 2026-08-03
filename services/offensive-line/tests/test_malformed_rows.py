"""One bad row is not a bad pass.

Every adapter parses a document nobody in this repo controls. A row with a
blank id, a non-numeric snap count or an unparseable rank has to be dropped as
it is read — the alternative is a `ValueError` out of a generator that is
`async for`-ed inside a fetch, which ends the whole capture over one cell.

The other direction matters as much: a document that is malformed *at the
header* must fail loudly, because mapping a million rows to nulls and calling
it a quiet week is the failure an append-only lake never recovers from. That
half lives in `test_adapters.py`; this file is the per-row half.
"""

import gzip
from collections.abc import Sequence

import httpx
import respx

from offensive_line.adapters import depth as depth_adapter
from offensive_line.adapters import injuries as injuries_adapter
from offensive_line.adapters import participation as participation_adapter
from offensive_line.adapters import pbp as pbp_adapter
from offensive_line.adapters import players as players_adapter
from offensive_line.adapters import snaps as snaps_adapter

from . import season as season_module
from .conftest import SEASON, WEEK, Feeds


def _row(columns: Sequence[str], **overrides) -> dict[str, str]:
    row = dict.fromkeys(columns, "")
    row.update(overrides)
    return row


async def _fetch(feeds: Feeds, call):
    with respx.mock(assert_all_called=False) as router:
        feeds.install(router)
        async with httpx.AsyncClient() as client:
            return await call(client)


async def test_a_pbp_row_with_an_unparseable_play_id_is_dropped():
    bad = _row(
        season_module.PBP_COLUMNS,
        game_id="2026_01_AAA_BBB",
        play_id="not-a-number",
        game_date=season_module.game_date(1),
        season_type="REG",
        week="1",
        posteam="AAA",
        defteam="BBB",
        qb_dropback="1",
    )
    feeds = Feeds(
        bodies={
            "pbp": season_module.pbp_document(
                season_module.build_season(), extra_rows=[bad]
            )
        }
    )
    fold = await _fetch(feeds, lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c))
    assert fold.dropbacks, "the rest of the document still parsed"
    assert not any(game == "2026_01_AAA_BBB" for game, _play in fold.dropbacks)


async def test_a_pbp_row_missing_a_team_is_dropped():
    """`posteam` is blank on a kickoff and on every timeout row. Neither is a
    scrimmage play and neither has a line to attribute anything to."""
    bad = _row(
        season_module.PBP_COLUMNS,
        game_id="2026_01_AAA_BBB",
        play_id="9001",
        season_type="REG",
        week="1",
        defteam="BBB",
        rush_attempt="1",
        rushing_yards="4",
    )
    feeds = Feeds(
        bodies={
            "pbp": season_module.pbp_document(
                season_module.build_season(), extra_rows=[bad]
            )
        }
    )
    fold = await _fetch(feeds, lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c))
    assert ("", "2026_01_AAA_BBB") not in fold.offense_game


async def test_na_and_blank_yardage_read_as_zero_not_as_a_crash():
    """The feed writes `NA` for a column a play does not have. `float("NA")`
    raises, and the raise would come out of the middle of a stream."""
    assert pbp_adapter._num("NA") == 0.0
    assert pbp_adapter._num("") == 0.0
    assert pbp_adapter._num("  ") == 0.0
    assert pbp_adapter._num("nonsense") == 0.0
    assert pbp_adapter._num("-3.5") == -3.5
    assert pbp_adapter._flag("1") is True
    assert pbp_adapter._flag("0") is False


async def test_a_participation_row_with_a_bad_rusher_count_is_dropped():
    bad = _row(
        season_module.PARTICIPATION_COLUMNS,
        nflverse_game_id="2026_01_AAA_BBB",
        play_id="9002",
        number_of_pass_rushers="four",
        was_pressure="TRUE",
    )
    feeds = Feeds(
        bodies={
            "participation": season_module.participation_document(
                season_module.build_season(), extra_rows=[bad]
            )
        }
    )
    snaps = await _fetch(
        feeds, lambda c: participation_adapter.fetch_block_snaps(SEASON, client=c)
    )
    assert ("2026_01_AAA_BBB", 9002) not in snaps
    assert snaps, "the rest of the document still parsed"


async def test_an_unparseable_release_time_is_null_rather_than_fatal():
    bad = _row(
        season_module.PARTICIPATION_COLUMNS,
        nflverse_game_id="2026_01_AAA_BBB",
        play_id="9003",
        number_of_pass_rushers="4",
        was_pressure="FALSE",
        time_to_throw="fast",
    )
    feeds = Feeds(
        bodies={
            "participation": season_module.participation_document(
                season_module.build_season(), extra_rows=[bad]
            )
        }
    )
    snaps = await _fetch(
        feeds, lambda c: participation_adapter.fetch_block_snaps(SEASON, client=c)
    )
    assert snaps[("2026_01_AAA_BBB", 9003)].time_to_throw is None


async def test_a_snap_row_with_a_non_numeric_count_is_dropped():
    body = gzip.decompress(season_module.snap_counts_document()).decode()
    lines = body.split("\n")
    header = lines[0].split(",")
    bad = _row(
        header,
        game_id="2026_01_AAA_BBB",
        season=str(SEASON),
        game_type="REG",
        week="1",
        player="Broken Row",
        pfr_player_id="BrokRo01",
        position="T",
        team="AAA",
        offense_snaps="many",
    )
    lines.insert(1, ",".join(bad[column] for column in header))
    feeds = Feeds(bodies={"snaps": gzip.compress("\n".join(lines).encode())})
    fold = await _fetch(
        feeds, lambda c: snaps_adapter.fetch_snaps(SEASON, WEEK, client=c)
    )
    assert not any(entry.pfr_id == "BrokRo01" for entry in fold.line)
    assert fold.line, "the rest of the document still parsed"


async def test_a_zero_snap_row_contributes_to_neither_output():
    """A defender or a special-teamer. Dropped as it is parsed rather than
    materialised and narrowed afterwards, and it must not become the team's
    snap denominator."""
    fold = await _fetch(
        Feeds(), lambda c: snaps_adapter.fetch_snaps(SEASON, WEEK, client=c)
    )
    assert all(entry.offense_snaps > 0 for entry in fold.line)
    assert all(total > 0 for total in fold.team_offense_snaps.values())


async def test_a_depth_row_with_an_unparseable_rank_is_dropped():
    body = gzip.decompress(season_module.depth_charts_document()).decode()
    lines = body.split("\n")
    header = lines[0].split(",")
    bad = _row(
        header,
        dt=f"{season_module.game_date(1)}T07:32:09Z",
        team="AAA",
        player_name="Broken",
        gsis_id="00-9999999",
        pos_abb="LT",
        pos_rank="first",
    )
    lines.insert(1, ",".join(bad[column] for column in header))
    feeds = Feeds(bodies={"depth": gzip.compress("\n".join(lines).encode())})
    charts = await _fetch(
        feeds, lambda c: depth_adapter.fetch_depth_charts(SEASON, client=c)
    )
    assert ("AAA", "00-9999999") not in charts.labels_at(season_module.game_date(1))
    assert charts.dates


async def test_a_depth_row_with_an_unusable_timestamp_is_dropped():
    """`dt` is the snapshot's identity. A row without a usable date cannot be
    placed on the calendar, and guessing would label a week from a chart that
    may postdate it by months."""
    body = gzip.decompress(season_module.depth_charts_document()).decode()
    lines = body.split("\n")
    header = lines[0].split(",")
    bad = _row(
        header, dt="soon", team="AAA", gsis_id="00-9999998", pos_abb="RT", pos_rank="1"
    )
    lines.insert(1, ",".join(bad[column] for column in header))
    feeds = Feeds(bodies={"depth": gzip.compress("\n".join(lines).encode())})
    charts = await _fetch(
        feeds, lambda c: depth_adapter.fetch_depth_charts(SEASON, client=c)
    )
    assert "soon" not in charts.dates
    assert all(len(date) == 10 for date in charts.dates)


async def test_a_player_row_with_an_unparseable_jersey_number_still_resolves():
    """The jersey is 0.20 of `player-identity`'s scoring weight, so losing it
    costs resolution quality — but dropping the whole player over it would
    cost the crosswalk entry, and with it a starter row."""
    body = gzip.decompress(season_module.players_document()).decode()
    lines = body.split("\n")
    header = lines[0].split(",")
    bad = _row(
        header,
        gsis_id="00-9999997",
        display_name="No Number",
        pfr_id="NoNumb01",
        position_group="OL",
        position="OT",
        jersey_number="TBD",
        status="ACT",
    )
    lines.insert(1, ",".join(bad[column] for column in header))
    feeds = Feeds(bodies={"players": gzip.compress("\n".join(lines).encode())})
    roster = await _fetch(feeds, lambda c: players_adapter.fetch_line_players(client=c))
    assert roster.by_gsis["00-9999997"].jersey_number is None
    assert roster.gsis_for_pfr["NoNumb01"] == "00-9999997"


async def test_an_injury_row_with_an_unknown_status_reads_as_active():
    """An unrecognised value falls through to `active` rather than being
    invented into a new enum value — the schema restricts the enum, so a
    fabricated status would fail conformance rather than reach the lake."""
    body = gzip.decompress(season_module.injuries_document()).decode()
    lines = body.split("\n")
    header = lines[0].split(",")
    bad = _row(
        header,
        season=str(SEASON),
        season_type="REG",
        game_type="REG",
        team="AAA",
        week=str(WEEK + 1),
        gsis_id=season_module.line_id("AAA", 4),
        position="OT",
        report_status="Probable",
    )
    lines.insert(1, ",".join(bad[column] for column in header))
    feeds = Feeds(bodies={"injuries": gzip.compress("\n".join(lines).encode())})
    availability = await _fetch(
        feeds,
        lambda c: injuries_adapter.fetch_availability(SEASON, WEEK, client=c),
    )
    assert availability[season_module.line_id("AAA", 4)] == "active"

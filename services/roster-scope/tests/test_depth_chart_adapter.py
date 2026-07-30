"""The depth-chart adapter: parsing, grouping, freshness, and schema drift."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from roster_scope.adapters.depth_chart import (
    DepthChartSchemaError,
    depth_chart_url,
    fetch_depth_charts,
    parse_depth_chart_csv,
)

from .conftest import DEPTH_HEADER, depth_csv, depth_row

FETCHED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def parse(rows, *, season=2026, week=1, header=DEPTH_HEADER):
    return parse_depth_chart_csv(
        depth_csv(rows, header), season=season, week=week, fetched_at=FETCHED_AT
    )


def test_groups_rows_by_canonical_team():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "Patrick Mahomes"),
            depth_row("JAC", "QB", 1, "Trevor Lawrence"),
        ]
    )
    assert set(charts) == {"KC", "JAX"}
    assert charts["JAX"].rows[0].name_raw == "Trevor Lawrence"


def test_rows_outside_the_scoped_season_or_week_are_ignored():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "In Scope", season=2026, week=1),
            depth_row("KC", "QB", 2, "Wrong Week", season=2026, week=2),
            depth_row("KC", "QB", 3, "Wrong Season", season=2025, week=1),
        ]
    )
    assert [r.name_raw for r in charts["KC"].rows] == ["In Scope"]


def test_unknown_team_is_dropped_rather_than_passed_through():
    """Its slots then read as missing, which is the correct accounting for
    'we have no chart for this team'."""
    charts = parse([depth_row("XYZ", "QB", 1, "Nobody")])
    assert charts == {}


def test_unordered_or_nameless_rows_are_dropped():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "Real Player"),
            "2026,1,KC,QB,,Missing Order,,",
            "2026,1,KC,QB,2,,,",
        ]
    )
    assert [r.name_raw for r in charts["KC"].rows] == ["Real Player"]


def test_jersey_number_is_parsed_and_tolerates_junk():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "A Player", jersey="15"),
            depth_row("KC", "QB", 2, "B Player", jersey="n/a"),
        ]
    )
    numbers = {r.name_raw: r.jersey_number for r in charts["KC"].rows}
    assert numbers == {"A Player": 15, "B Player": None}


def test_captured_at_is_the_newest_last_updated_for_that_team():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "A", last_updated="2026-09-14T08:00:00Z"),
            depth_row("KC", "QB", 2, "B", last_updated="2026-09-15T09:30:00Z"),
            depth_row("BUF", "QB", 1, "C", last_updated="2026-09-01T00:00:00Z"),
        ]
    )
    assert charts["KC"].captured_at == datetime(2026, 9, 15, 9, 30, tzinfo=UTC)
    # Per team, not one number for the fetch — one frozen chart must stay
    # visible rather than being averaged away.
    assert charts["BUF"].captured_at == datetime(2026, 9, 1, tzinfo=UTC)


def test_captured_at_falls_back_to_the_fetch_instant():
    """A feed without the column is as fresh as the fetch. Inventing an old
    timestamp would make every team permanently stale."""
    charts = parse([depth_row("KC", "QB", 1, "A")])
    assert charts["KC"].captured_at == FETCHED_AT


def test_unparseable_last_updated_falls_back_rather_than_raising():
    charts = parse([depth_row("KC", "QB", 1, "A", last_updated="last tuesday")])
    assert charts["KC"].captured_at == FETCHED_AT


def test_missing_required_column_fails_loudly():
    """Schema drift must fail the capture with a classified reason, not map
    nulls into an append-only lake."""
    header = "season,week,club_code,depth_position,depth_team"  # no full_name
    with pytest.raises(DepthChartSchemaError) as exc:
        parse(["2026,1,KC,QB,1"], header=header)
    assert "full_name" in str(exc.value)


def test_schema_error_is_a_value_error_so_it_classifies_as_malformed():
    """`CollectorMetrics.reason_for` maps ValueError to `malformed`; if this
    stopped being a ValueError the reason label would silently become
    `unknown`."""
    assert issubclass(DepthChartSchemaError, ValueError)


def test_depth_chart_url_substitutes_the_season(monkeypatch):
    monkeypatch.setattr(
        "roster_scope.adapters.depth_chart.DEPTH_CHART_URL",
        "https://feed.test/{season}.csv",
    )
    assert depth_chart_url(2027) == "https://feed.test/2027.csv"


@respx.mock
async def test_fetch_uses_the_season_url_and_parses(monkeypatch):
    monkeypatch.setattr(
        "roster_scope.adapters.depth_chart.DEPTH_CHART_URL",
        "https://feed.test/{season}.csv",
    )
    respx.get("https://feed.test/2026.csv").mock(
        return_value=httpx.Response(
            200, text=depth_csv([depth_row("KC", "QB", 1, "Patrick Mahomes")])
        )
    )
    async with httpx.AsyncClient() as client:
        charts = await fetch_depth_charts(2026, 1, client, now=FETCHED_AT)
    assert charts["KC"].rows[0].name_raw == "Patrick Mahomes"


@respx.mock
async def test_fetch_raises_on_an_error_status(monkeypatch):
    monkeypatch.setattr(
        "roster_scope.adapters.depth_chart.DEPTH_CHART_URL",
        "https://feed.test/{season}.csv",
    )
    respx.get("https://feed.test/2026.csv").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_depth_charts(2026, 1, client, now=FETCHED_AT)


def test_naive_last_updated_is_read_as_utc():
    charts = parse([depth_row("KC", "QB", 1, "A", last_updated="2026-09-14T08:00:00")])
    assert charts["KC"].captured_at == FETCHED_AT - timedelta(hours=28)

"""The wire paths, exercised against a mocked upstream rather than the stub.

The stub week keeps the rest of the suite offline and deterministic, but it is
also the one thing that cannot catch a parsing bug: it never goes through a
line of CSV. These tests do.

They also pin the two rules that cost `roster-scope` a production deploy —
stream the response, and filter to what is kept **as** it is parsed — by
asserting on what the adapters retain rather than on what the feed contained.
"""

import httpx
import pytest
import respx
from collector_core.streaming import UpstreamSchemaError

from injury_report.adapters.schedule import (
    REQUIRED_COLUMNS as SCHEDULE_COLUMNS,
)
from injury_report.adapters.schedule import (
    fetch_scheduled_games,
)
from injury_report.adapters.upstream import (
    REQUIRED_COLUMNS as REPORT_COLUMNS,
)
from injury_report.adapters.upstream import (
    fetch_report_rows,
    source_ref,
    upstream_url,
)

SCHEDULE_URL = "https://upstream.test/games.csv"
REPORT_URL = "https://upstream.test/injuries/{season}-{week}.csv"


def _csv(columns, rows) -> str:
    header = ",".join(columns)
    body = "\n".join(",".join(str(row[column]) for column in columns) for row in rows)
    return f"{header}\n{body}\n"


@pytest.fixture
def schedule_env(monkeypatch):
    monkeypatch.setenv("SCHEDULE_URL", SCHEDULE_URL)


@pytest.fixture
def report_env(monkeypatch):
    monkeypatch.setenv("INJURY_REPORT_URL", REPORT_URL)


def _game(season, week, game_id, home, away) -> dict:
    return {
        "season": season,
        "week": week,
        "game_id": game_id,
        "home_team": home,
        "away_team": away,
    }


SCHEDULE_ROWS = [
    _game(2026, 1, "g1", "KC", "BUF"),
    _game(2026, 1, "g2", "SF", "SEA"),
    _game(2026, 2, "g9", "NE", "NYJ"),  # another week
    _game(2025, 1, "g0", "DAL", "PHI"),  # another season
]


@respx.mock
async def test_the_schedule_adapter_keeps_only_the_scoped_week(schedule_env):
    """Filtered during the parse, not after. The real feed is every game since
    1999; materialising it to keep sixteen rows is the shape that OOM-killed
    roster-scope at a 256Mi limit."""
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(
            200, text=_csv(sorted(SCHEDULE_COLUMNS), SCHEDULE_ROWS)
        )
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_scheduled_games(2026, 1, client=client)
    assert games == {"KC": "g1", "BUF": "g1", "SF": "g2", "SEA": "g2"}


@respx.mock
async def test_a_renamed_schedule_column_fails_loudly(schedule_env):
    """Schema drift must fail the capture with `reason=schema` rather than map
    nulls into an append-only lake nobody rewrites."""
    columns = [c if c != "home_team" else "home" for c in sorted(SCHEDULE_COLUMNS)]
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=",".join(columns) + "\n")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamSchemaError):
            await fetch_scheduled_games(2026, 1, client=client)


@respx.mock
async def test_the_schedule_adapter_raises_rather_than_returning_nothing(schedule_env):
    """An empty mapping would mean 'no club owes a report', which reads as
    complete coverage of nothing."""
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_scheduled_games(2026, 1, client=client)


def _report_row(**overrides):
    base = {column: "" for column in REPORT_COLUMNS}
    base.update(
        {
            "season": "2026",
            "week": "1",
            "team": "KC",
            "practice_day": "Wed",
            "report_published_at": "2026-09-16T21:00:00Z",
            "is_final_report": "false",
            "is_estimated": "false",
        }
    )
    base.update(overrides)
    return base


@respx.mock
async def test_the_report_adapter_filters_team_week_and_day_as_it_parses(report_env):
    """Three filters, all applied during the parse. A row for another club,
    another week or a day this pass is not capturing is never retained."""
    rows = [
        _report_row(player_external_id="kc-1"),
        _report_row(team="SEA", player_external_id="sea-1"),
        _report_row(week="2", player_external_id="kc-2"),
        _report_row(practice_day="friday", player_external_id="kc-3"),
    ]
    respx.get(REPORT_URL.format(season=2026, week=1)).mock(
        return_value=httpx.Response(200, text=_csv(sorted(REPORT_COLUMNS), rows))
    )
    async with httpx.AsyncClient() as client:
        kept = await fetch_report_rows(
            2026, 1, client=client, teams=["KC", "BUF"], days=["wednesday"]
        )
    assert len(kept) == 1
    assert kept[0]["player_external_id"] == "kc-1"


@respx.mock
async def test_a_renamed_report_column_fails_loudly(report_env):
    columns = [c if c != "report_status" else "status" for c in sorted(REPORT_COLUMNS)]
    respx.get(REPORT_URL.format(season=2026, week=1)).mock(
        return_value=httpx.Response(200, text=",".join(columns) + "\n")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamSchemaError):
            await fetch_report_rows(
                2026, 1, client=client, teams=["KC"], days=["wednesday"]
            )


def test_source_ref_is_none_in_stub_mode_and_the_exact_artifact_otherwise(monkeypatch):
    """`source_ref` is what makes a lake object reproducible a season later."""
    monkeypatch.delenv("INJURY_REPORT_URL", raising=False)
    assert upstream_url() == ""
    assert source_ref(2026, 1) is None

    monkeypatch.setenv("INJURY_REPORT_URL", REPORT_URL)
    assert source_ref(2026, 4) == "https://upstream.test/injuries/2026-4.csv"

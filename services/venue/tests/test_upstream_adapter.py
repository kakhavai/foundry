"""The schedule adapter — the only module that knows the game feed's wire format.

`venue_static` has no adapter to test: it reads a committed table, which is the
whole reason this collector was built on one. Everything here is about the CSV.
"""

from datetime import UTC, date, datetime

import httpx
import pytest

from venue.adapters.upstream import (
    EXCLUDED_GAME_TYPES,
    REQUIRED_COLUMNS,
    fetch_season_games,
    parse_kickoff_date,
    utc_today,
)

from .conftest import SEASON, game_row, mock_upstream, season_rows, to_csv


async def _fetch(csv: str, season: int = SEASON):
    with mock_upstream(csv):
        async with httpx.AsyncClient() as client:
            return await fetch_season_games(season, client=client)


async def test_only_the_requested_season_survives_the_stream():
    """Filtered as the document is parsed, never materialised first. The real
    feed is every game since 1999 in one file; a capture keeps one season."""
    rows = [
        *season_rows(weeks=1, season=SEASON),
        *season_rows(weeks=1, season=SEASON - 1),
    ]
    games = await _fetch(to_csv(rows))
    assert len(games) == 16
    assert {game.season for game in games} == {SEASON}


async def test_preseason_is_excluded():
    rows = [
        game_row(week=1, away="CHI", home="GB"),
        game_row(week=1, away="MIN", home="DET", game_type="PRE"),
    ]
    games = await _fetch(to_csv(rows))
    assert [game.game_type for game in games] == ["REG"]
    assert EXCLUDED_GAME_TYPES == frozenset({"PRE"})


async def test_the_neutral_site_flag_comes_from_location_not_the_stadium_id():
    """Carried over from `schedule_context`: this flag, not the stadium id, is
    what says the id describes the home CLUB rather than the venue."""
    rows = [
        game_row(week=1, away="CHI", home="GB"),
        game_row(
            week=1,
            away="MIN",
            home="JAX",
            location="Neutral",
            stadium="Wembley Stadium",
        ),
    ]
    games = await _fetch(to_csv(rows))
    assert [game.is_neutral_site for game in games] == [False, True]
    assert games[1].stadium_name == "Wembley Stadium"


async def test_a_renamed_column_fails_the_capture_loudly():
    """Schema drift must fail with `reason=malformed` rather than map nulls
    into an append-only lake. Asserted before a single row is yielded."""
    csv = to_csv(season_rows(weeks=1)).replace("gameday", "game_date", 1)
    with pytest.raises(Exception) as excinfo:
        await _fetch(csv)
    assert "gameday" in str(excinfo.value)


async def test_an_http_failure_raises_rather_than_returning_an_empty_list():
    """An empty list would be recorded as a successful capture of nothing."""
    with mock_upstream("", status=503):
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_season_games(SEASON, client=client)


def test_required_columns_are_exactly_what_the_mapping_reads():
    """No more, so an unrelated column disappearing upstream does not fail a
    capture that never used it."""
    assert REQUIRED_COLUMNS == frozenset(
        {
            "game_id",
            "season",
            "game_type",
            "week",
            "gameday",
            "home_team",
            "location",
            "stadium",
        }
    )


def test_an_absent_or_unparseable_kickoff_date_is_none_not_a_raise():
    """One unslotted game must not fail a whole season's capture."""
    assert parse_kickoff_date("2026-09-13") == date(2026, 9, 13)
    assert parse_kickoff_date("") is None
    assert parse_kickoff_date("   ") is None
    assert parse_kickoff_date("13/09/2026") is None
    assert parse_kickoff_date("not-a-date") is None


def test_utc_today_comes_from_the_captures_own_clock():
    """Not `date.today()`: a test that freezes the clock must freeze this too,
    and a capture must stay reproducible from its own envelope."""
    assert utc_today(datetime(2026, 9, 15, 12, 0, tzinfo=UTC)) == date(2026, 9, 15)
    # An instant late enough that the UTC date has already rolled over.
    assert utc_today(datetime(2026, 9, 15, 23, 59, tzinfo=UTC)) == date(2026, 9, 15)

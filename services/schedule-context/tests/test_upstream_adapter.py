"""The adapter: what it reads, what it refuses, and what it does not buffer.

The feed is every game since 1999 in one CSV. A capture keeps one season of
it, and the two ways that goes wrong are both invisible from the outside:
reading the whole document into memory three times over (which OOM-killed
`roster-scope` at a 256Mi limit), and mapping a renamed column to nulls.
"""

import httpx
import pytest
from collector_core.streaming import UpstreamSchemaError

from schedule_context.adapters.upstream import (
    EXCLUDED_GAME_TYPES,
    REQUIRED_COLUMNS,
    UPSTREAM_URL,
    fetch_season_games,
    parse_kickoff,
)

from .conftest import COLUMNS, game_row, mock_upstream, season_rows, to_csv


async def fetch(csv: str, season: int = 2026):
    with mock_upstream(csv):
        async with httpx.AsyncClient() as client:
            return await fetch_season_games(season, client=client)


async def test_only_the_requested_season_survives_the_stream():
    """The out-of-scope rows are discarded as they go past. Keeping ~285 of
    ~7,000 by materialising all 7,000 first is the shape that OOM-killed a
    neighbour."""
    rows = season_rows(weeks=2, season=2026) + season_rows(weeks=2, season=2025)
    games = await fetch(to_csv(rows), season=2026)
    assert len(games) == 32
    assert {g.season for g in games} == {2026}


async def test_preseason_games_are_excluded():
    """Counting them would insert a phantom rest gap into every club's week-1
    chain."""
    rows = season_rows(weeks=1) + [
        game_row(week=1, away="BUF", home="NYJ", game_type="PRE")
    ]
    games = await fetch(to_csv(rows))
    assert "PRE" in EXCLUDED_GAME_TYPES
    assert len(games) == 16
    assert {g.game_type for g in games} == {"REG"}


async def test_a_renamed_column_fails_the_capture_loudly():
    """Schema drift must raise with `reason=malformed` rather than map nulls
    into an append-only lake nobody rewrites."""
    csv = to_csv(season_rows(weeks=1)).replace("gametime", "kickoff_time", 1)
    with pytest.raises(UpstreamSchemaError, match="gametime"):
        await fetch(csv)


async def test_the_required_columns_are_exactly_what_the_mapping_reads():
    """Requiring a column the mapping never reads fails captures for no
    reason; requiring too few maps nulls silently."""
    assert REQUIRED_COLUMNS == set(COLUMNS)
    assert len(REQUIRED_COLUMNS) == 10


async def test_an_upstream_error_status_raises_rather_than_returning_nothing():
    """An empty list would be recorded as a successful capture of nothing."""
    with mock_upstream("", status=500):
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_season_games(2026, client=client)


async def test_a_neutral_site_row_keeps_the_stadium_name():
    games = await fetch(
        to_csv(
            [
                game_row(
                    week=1,
                    away="BUF",
                    home="JAX",
                    location="Neutral",
                    stadium="Wembley Stadium",
                )
            ]
        )
    )
    assert len(games) == 1
    assert games[0].is_neutral_site is True
    assert games[0].stadium_name == "Wembley Stadium"


def test_the_upstream_url_is_environment_overridable():
    """A load test or a fixture server must be able to stand in for the real
    feed without hammering a third party."""
    assert UPSTREAM_URL.startswith("http")


@pytest.mark.parametrize(
    "gameday,gametime",
    [("", "13:00"), ("2026-09-13", ""), ("2026-09-13", "afternoon"), ("nope", "13:00")],
)
def test_an_unusable_kickoff_parses_to_none_rather_than_raising(gameday, gametime):
    """One unslotted game must not fail a whole season's capture; the caller
    records it as missing with a reason."""
    assert parse_kickoff(gameday, gametime) is None

from datetime import UTC, datetime

import httpx
import pytest
import respx

from weather.adapters.schedule import (
    SCHEDULE_URL,
    ScheduledGame,
    fetch_schedule,
    parse_schedule_csv,
)

HEADER = (
    "game_id,season,game_type,week,gameday,gametime,away_team,home_team,"
    "location,roof,surface,stadium_id,stadium"
)


def csv_rows(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


SEPTEMBER_GAME = (
    "2026_01_CHI_CAR,2026,REG,1,2026-09-13,13:00,CHI,CAR,Home,outdoors,grass,CAR00,"
    "Bank of America Stadium"
)
NOVEMBER_GAME = (
    "2026_11_ARI_SEA,2026,REG,11,2026-11-22,13:00,ARI,SEA,Home,outdoors,fieldturf,"
    "SEA00,Lumen Field"
)
RETRACTABLE_GAME = (
    "2026_01_BUF_HOU,2026,REG,1,2026-09-13,13:00,BUF,HOU,Home,,astroturf,HOU00,"
    "Reliant Stadium"
)
MUNICH_GAME = (
    "2026_10_NE_DET,2026,REG,10,2026-11-15,09:30,NE,DET,Neutral,dome,grass,DET00,"
    "FC Bayern Munich Stadium"
)


def test_parses_a_september_kickoff_from_eastern_to_utc():
    """13:00 ET during daylight time is 17:00 UTC."""
    (game,) = parse_schedule_csv(csv_rows(SEPTEMBER_GAME), season=2026, week=1)
    assert game.kickoff_at == datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def test_parses_a_november_kickoff_across_the_dst_boundary():
    """13:00 ET after the November transition is 18:00 UTC, not 17:00.
    A fixed offset gets exactly the late-season games wrong."""
    (game,) = parse_schedule_csv(csv_rows(NOVEMBER_GAME), season=2026, week=11)
    assert game.kickoff_at == datetime(2026, 11, 22, 18, 0, tzinfo=UTC)


def test_extracts_the_documented_fields():
    (game,) = parse_schedule_csv(csv_rows(SEPTEMBER_GAME), season=2026, week=1)
    assert game == ScheduledGame(
        game_id="2026_01_CHI_CAR",
        season=2026,
        week=1,
        kickoff_at=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        home_team="CAR",
        away_team="CHI",
        stadium_id="CAR00",
        stadium_name="Bank of America Stadium",
        is_neutral_site=False,
        roof_raw="outdoors",
    )


def test_empty_roof_becomes_none_not_empty_string():
    (game,) = parse_schedule_csv(csv_rows(RETRACTABLE_GAME), season=2026, week=1)
    assert game.roof_raw is None


def test_neutral_site_discards_stadium_id_and_roof():
    """The feed reports the DESIGNATED HOME TEAM's venue for neutral sites.
    Trusting it fetches Detroit's weather for a game played in Munich."""
    (game,) = parse_schedule_csv(csv_rows(MUNICH_GAME), season=2026, week=10)
    assert game.is_neutral_site is True
    assert game.stadium_id is None
    assert game.roof_raw is None
    assert game.stadium_name == "FC Bayern Munich Stadium"


def test_filters_to_the_requested_week():
    text = csv_rows(SEPTEMBER_GAME, NOVEMBER_GAME)
    assert len(parse_schedule_csv(text, season=2026, week=1)) == 1


def test_filters_to_the_requested_season():
    older = SEPTEMBER_GAME.replace("2026_01_CHI_CAR,2026", "2025_01_CHI_CAR,2025")
    text = csv_rows(SEPTEMBER_GAME, older)
    games = parse_schedule_csv(text, season=2026, week=1)
    assert [g.game_id for g in games] == ["2026_01_CHI_CAR"]


def test_a_row_with_an_unparseable_kickoff_is_rejected_loudly():
    bad = SEPTEMBER_GAME.replace(",13:00,", ",not-a-time,")
    with pytest.raises(ValueError, match="kickoff"):
        parse_schedule_csv(csv_rows(bad), season=2026, week=1)


@respx.mock
async def test_fetch_schedule_calls_the_upstream_and_parses():
    respx.get(SCHEDULE_URL).mock(
        return_value=httpx.Response(200, text=csv_rows(SEPTEMBER_GAME))
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_schedule(2026, 1, client)
    assert [g.game_id for g in games] == ["2026_01_CHI_CAR"]


@respx.mock
async def test_fetch_schedule_raises_on_upstream_error():
    respx.get(SCHEDULE_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_schedule(2026, 1, client)

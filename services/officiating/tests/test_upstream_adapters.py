"""The three adapters, at the wire format.

Everything here is a fact about bytes rather than about orchestration: the
crosswalk, the on-field filter, the gzip inflater, the penalty aggregation and
the conditional-GET opt-in. A test that started from parsed rows could see none
of them, and all five are places this collector can be silently wrong.
"""

import httpx
import pytest
import respx
from collector_core.conditional import ETAGS, UpstreamUnchanged
from collector_core.streaming import UpstreamSchemaError

from officiating.adapters import games as games_adapter
from officiating.adapters import officials as officials_adapter
from officiating.adapters import pbp as pbp_adapter

from .conftest import (
    DEFAULT_PENALTIES,
    SEASON,
    FakeGame,
    games_csv,
    gzipped,
    officials_csv,
    pbp_csv,
    season_of,
)

ONE_GAME = [
    FakeGame(
        game_id=f"{SEASON}_01_DAL_PHI",
        legacy_game_id=f"{SEASON}090400",
        week=1,
        referee_id="758",
        referee_name="Shawn Smith",
    )
]


async def _fetch(coroutine):
    async with httpx.AsyncClient() as client:
        return await coroutine(client)


# ---------------------------------------------------------------------------
# games.csv — the crosswalk
# ---------------------------------------------------------------------------


@respx.mock
async def test_the_schedule_adapter_carries_both_ids():
    """The finding the whole collector rests on: `old_game_id` is published,
    so the spec's "hand-maintained crosswalk" is not needed."""
    respx.get(games_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=games_csv(ONE_GAME))
    )

    games = await _fetch(lambda c: games_adapter.fetch_season_games(SEASON, client=c))

    assert len(games) == 1
    assert games[0].game_id == f"{SEASON}_01_DAL_PHI"
    assert games[0].legacy_game_id == f"{SEASON}090400"
    assert games[0].referee == "Shawn Smith"
    assert games[0].week == 1


@respx.mock
async def test_a_row_with_no_old_game_id_is_dropped():
    """It cannot be joined to the officials feed at all, and a blank key would
    collide with every other blank — silently mapping many games onto one."""
    text = games_csv(ONE_GAME).replace(f"{SEASON}090400", "")
    respx.get(games_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=text)
    )

    assert (
        await _fetch(lambda c: games_adapter.fetch_season_games(SEASON, client=c)) == []
    )


@respx.mock
async def test_other_seasons_and_the_postseason_are_filtered_out():
    """Postseason crews are assembled on merit from across the regular
    season's crews, so a January game's seven officials are mostly not the crew
    whose season it would extend. Folding them in would attribute a reshuffled
    group's penalties to a crew that no longer exists."""
    text = games_csv(ONE_GAME)
    text += (
        f"{SEASON}_20_A_B,{SEASON},WC,20,{SEASON}-01-10,A,B,"
        f"{SEASON}011000,Some Ref,Some Stadium\n"
    )
    text += (
        f"{SEASON - 1}_01_A_B,{SEASON - 1},REG,1,{SEASON - 1}-09-01,A,B,"
        f"{SEASON - 1}090100,Some Ref,Some Stadium\n"
    )
    respx.get(games_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=text)
    )

    games = await _fetch(lambda c: games_adapter.fetch_season_games(SEASON, client=c))

    assert [g.game_id for g in games] == [f"{SEASON}_01_DAL_PHI"]


@respx.mock
async def test_a_renamed_schedule_column_fails_loudly():
    """Schema drift must fail the capture rather than map nulls into an
    append-only lake that is never rewritten."""
    text = games_csv(ONE_GAME).replace("old_game_id", "legacy_id", 1)
    respx.get(games_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=text)
    )

    with pytest.raises(UpstreamSchemaError, match="old_game_id"):
        await _fetch(lambda c: games_adapter.fetch_season_games(SEASON, client=c))


# ---------------------------------------------------------------------------
# officials.csv
# ---------------------------------------------------------------------------


@respx.mock
async def test_the_officials_adapter_keeps_only_the_on_field_crew():
    """The replay official the feed also lists throws no flags and appears
    irregularly. Counted, every crew is eight people and continuity reads as
    churn on exactly the games where one happened to be recorded."""
    respx.get(officials_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=officials_csv(ONE_GAME))
    )

    crews = await _fetch(
        lambda c: officials_adapter.fetch_season_officials(SEASON, client=c)
    )

    members = crews[f"{SEASON}090400"]
    assert len(members) == 7
    assert {m.position for m in members} == {
        "Referee",
        "Umpire",
        "Down Judge",
        "Line Judge",
        "Field Judge",
        "Side Judge",
        "Back Judge",
    }
    assert not any(m.position == "Replay Official" for m in members)


@respx.mock
async def test_an_official_with_no_id_is_dropped():
    """An official with no id cannot be tracked across games, so counting them
    towards continuity would credit stability this collector cannot observe.
    The name is not a fallback — that is the whole lesson of the referee
    cross-check."""
    text = officials_csv(ONE_GAME).replace(",758,", ",,", 1)
    respx.get(officials_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=text)
    )

    crews = await _fetch(
        lambda c: officials_adapter.fetch_season_officials(SEASON, client=c)
    )

    assert len(crews[f"{SEASON}090400"]) == 6
    assert not any(m.position == "Referee" for m in crews[f"{SEASON}090400"])


@respx.mock
async def test_the_officials_adapter_is_keyed_by_the_legacy_game_id():
    """Legacy, because that is what this feed speaks. Translating inside the
    adapter would put the join in the one module that cannot see the
    schedule."""
    respx.get(officials_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=officials_csv(ONE_GAME))
    )

    crews = await _fetch(
        lambda c: officials_adapter.fetch_season_officials(SEASON, client=c)
    )

    assert set(crews) == {f"{SEASON}090400"}


# ---------------------------------------------------------------------------
# play-by-play — gzip, projection, aggregation
# ---------------------------------------------------------------------------


@respx.mock
async def test_the_pbp_adapter_inflates_and_aggregates():
    """Totals stated here independently of the builder: `DEFAULT_PENALTIES` is
    5 penalties worth 45 yards, of which 1 DPI worth 15, 2 offensive holdings
    and 1 defensive holding. A fixture carrying the totals directly would make
    this assertion tautological."""
    assert len(DEFAULT_PENALTIES) == 5, "the fixture changed; restate the totals"
    respx.get(pbp_adapter.source_ref(SEASON)).mock(
        return_value=httpx.Response(200, content=gzipped(pbp_csv(ONE_GAME)))
    )

    per_game = await _fetch(
        lambda c: pbp_adapter.fetch_season_penalties(SEASON, client=c)
    )

    game = per_game[f"{SEASON}_01_DAL_PHI"]
    assert game.penalties == 5
    assert game.penalty_yards == 45.0
    assert game.dpi == 1
    assert game.dpi_yards == 15.0
    assert game.offensive_holding == 2
    assert game.defensive_holding == 1
    assert game.offensive_plays == 120


@respx.mock
async def test_the_pbp_adapter_is_keyed_by_the_modern_game_id():
    """Play-by-play speaks the modern id and officials.csv speaks the legacy
    one; the crosswalk is what joins them, and getting this backwards produces
    an empty rate window rather than an error."""
    respx.get(pbp_adapter.source_ref(SEASON)).mock(
        return_value=httpx.Response(200, content=gzipped(pbp_csv(ONE_GAME)))
    )

    per_game = await _fetch(
        lambda c: pbp_adapter.fetch_season_penalties(SEASON, client=c)
    )

    assert set(per_game) == {f"{SEASON}_01_DAL_PHI"}


@respx.mock
async def test_a_penalty_with_no_enforced_yardage_still_counts():
    """Offsetting fouls and losses of down carry a blank `penalty_yards`. The
    penalty is real; only its yardage is absent, and raising would drop a whole
    game over one row."""
    game = FakeGame(
        game_id="g1",
        legacy_game_id="L1",
        week=1,
        referee_id="758",
        referee_name="Shawn Smith",
        penalties=(("Illegal Formation", ""),),
        offensive_plays=10,
    )
    respx.get(pbp_adapter.source_ref(SEASON)).mock(
        return_value=httpx.Response(200, content=gzipped(pbp_csv([game])))
    )

    per_game = await _fetch(
        lambda c: pbp_adapter.fetch_season_penalties(SEASON, client=c)
    )

    assert per_game["g1"].penalties == 1
    assert per_game["g1"].penalty_yards == 0.0


@respx.mock
async def test_only_offensive_snaps_count_as_plays():
    """ "Total offensive snaps", per the spec. A crew's share of special-teams
    plays is a property of the games' scorelines rather than of the crew, and
    the penalty rows here are `no_play` — counting those would double-count
    every flag as a snap."""
    respx.get(pbp_adapter.source_ref(SEASON)).mock(
        return_value=httpx.Response(200, content=gzipped(pbp_csv(ONE_GAME)))
    )

    per_game = await _fetch(
        lambda c: pbp_adapter.fetch_season_penalties(SEASON, client=c)
    )

    # 120 snaps and 5 penalty rows in the fixture; the penalties are not snaps.
    assert per_game[f"{SEASON}_01_DAL_PHI"].offensive_plays == 120


@respx.mock
async def test_a_renamed_penalty_column_fails_loudly():
    text = pbp_csv(ONE_GAME).replace("penalty_type", "foul_type", 1)
    respx.get(pbp_adapter.source_ref(SEASON)).mock(
        return_value=httpx.Response(200, content=gzipped(text))
    )

    with pytest.raises(UpstreamSchemaError, match="penalty_type"):
        await _fetch(lambda c: pbp_adapter.fetch_season_penalties(SEASON, client=c))


@respx.mock
async def test_the_pbp_url_is_scoped_to_the_season():
    """One file per season, unlike the other two feeds — which is also what
    keeps the ETag store bounded: one entry per season captured, not one per
    poll."""
    assert str(SEASON) in pbp_adapter.source_ref(SEASON)
    assert pbp_adapter.source_ref(SEASON) != pbp_adapter.source_ref(SEASON + 1)


# ---------------------------------------------------------------------------
# conditional GET
# ---------------------------------------------------------------------------


@respx.mock
async def test_each_adapter_sends_if_none_match_on_a_second_pass():
    """Cost, as a first-class constraint. The three feeds are 1.3 MB, 2.1 MB
    and 18.2 MiB gzipped, and a `weekly` cadence polls daily — so six of every
    seven polls should transfer nothing."""
    games = season_of(crews=1, weeks=1)
    routes = {
        games_adapter.UPSTREAM_URL: httpx.Response(
            200, text=games_csv(games), headers={"ETag": 'W/"g1"'}
        ),
        officials_adapter.UPSTREAM_URL: httpx.Response(
            200, text=officials_csv(games), headers={"ETag": 'W/"o1"'}
        ),
        pbp_adapter.source_ref(SEASON): httpx.Response(
            200, content=gzipped(pbp_csv(games)), headers={"ETag": 'W/"p1"'}
        ),
    }
    for url, response in routes.items():
        respx.get(url).mock(return_value=response)

    async with httpx.AsyncClient() as client:
        await games_adapter.fetch_season_games(SEASON, client=client)
        await officials_adapter.fetch_season_officials(SEASON, client=client)
        await pbp_adapter.fetch_season_penalties(SEASON, client=client)

    assert ETAGS.get(games_adapter.UPSTREAM_URL) == 'W/"g1"'
    assert ETAGS.get(officials_adapter.UPSTREAM_URL) == 'W/"o1"'
    assert ETAGS.get(pbp_adapter.source_ref(SEASON)) == 'W/"p1"'

    # Second pass: the store is primed, so every request carries the header.
    respx.reset()
    for url in routes:
        respx.get(url).mock(return_value=httpx.Response(304))

    async with httpx.AsyncClient() as client:
        for fetch in (
            lambda c: games_adapter.fetch_season_games(SEASON, client=c),
            lambda c: officials_adapter.fetch_season_officials(SEASON, client=c),
            lambda c: pbp_adapter.fetch_season_penalties(SEASON, client=c),
        ):
            with pytest.raises(UpstreamUnchanged):
                await fetch(client)


@respx.mock
async def test_a_failed_read_does_not_commit_an_etag():
    """An ETag claims the whole document was read. Commit one for a body that
    died partway and every later pass 304s: staleness resets, the failure
    counter stops moving, and the collector reports itself healthy on a
    truncated document until the upstream republishes."""
    text = games_csv(ONE_GAME).replace("old_game_id", "legacy_id", 1)
    respx.get(games_adapter.UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=text, headers={"ETag": 'W/"bad"'})
    )

    with pytest.raises(UpstreamSchemaError):
        await _fetch(lambda c: games_adapter.fetch_season_games(SEASON, client=c))

    assert ETAGS.get(games_adapter.UPSTREAM_URL) is None

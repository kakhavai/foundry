"""The wire format, the window, and the cost controls.

Two of these are cost tests rather than correctness tests, and they are the ones
that would otherwise never be written: conditional GET only saves anything if
BOTH halves are wired (the `etag_key` and the memo behind it), and the whole
43.8 MB cold window is only affordable because every pass after the first is a
handful of `304`s.
"""

import httpx
import pytest
import respx

from durability_history.adapters import upstream
from durability_history.adapters.upstream import (
    FIRST_AVAILABLE_SEASON,
    history_seasons,
    normalize_position,
    source_ref,
)

from .conftest import (
    HISTORY_SEASONS,
    SEASON,
    games_csv,
    injuries_csv,
    players_csv,
    snap_counts_csv,
)

# ── the window ───────────────────────────────────────────────────────────────


def test_the_window_includes_the_scoped_season_and_reaches_back():
    assert history_seasons(2026, span=3) == [2024, 2025, 2026]
    assert history_seasons(2026, span=1) == [2026]


def test_the_window_is_clamped_at_the_first_published_season():
    """nflverse publishes injury tables from 2009. A larger span would spend one
    request per year on documents that have never existed, and each would be
    recorded as a missing season forever."""
    assert history_seasons(2026, span=100)[0] == FIRST_AVAILABLE_SEASON


def test_a_span_below_one_is_a_hard_error():
    with pytest.raises(ValueError):
        history_seasons(2026, span=0)


def test_source_ref_names_every_artifact_the_pass_read():
    """A durability row is a join across five feeds, and a consumer tracing a
    wrong `days_to_return` a season later needs to know which documents produced
    it. The window is recoverable from this alone."""
    ref = source_ref(SEASON, 3, span=3)
    assert upstream.GAMES_URL in ref
    assert upstream.PLAYERS_URL in ref
    for season in HISTORY_SEASONS:
        assert upstream.INJURIES_URL.format(season=season) in ref
        assert upstream.SNAP_COUNTS_URL.format(season=season) in ref
        assert upstream.STATS_URL.format(season=season) in ref


def test_the_recurrence_rule_is_part_of_the_adapter_label():
    """The spec requires it: a rule change has to be distinguishable from a
    change in the player."""
    assert upstream.UPSTREAM_ADAPTER.startswith("nflverse-injury-tables")
    assert f"recurrence={upstream.RECURRENCE_RULE_VERSION}" in upstream.UPSTREAM_ADAPTER
    assert f"{upstream.RECURRENCE_WINDOW_DAYS}d_of_return" in upstream.UPSTREAM_ADAPTER


# ── parsing ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_a_game_with_no_result_is_not_a_game_anybody_could_have_missed():
    """Counting a scheduled future game inflates `career_games_possible`, which
    LOWERS `availability_rate` — manufacturing exactly the durability problem the
    named failure mode is about. The bias direction is why this is a hard filter.
    """
    respx.mock.get(upstream.GAMES_URL).respond(
        200, text=games_csv(seasons=[2026], unplayed_from=4)
    )
    async with httpx.AsyncClient() as client:
        schedule = await upstream.fetch_schedule([2026], client=client)

    weeks = [game.week for game in schedule.games(2026, "SEA")]
    assert weeks == [1, 2, 3]


@respx.mock
async def test_the_schedule_is_filtered_to_the_window_as_it_streams():
    """`games.csv` is every season from 1999. Keeping the ones outside the window
    would hold 27 seasons of games for a three-season history."""
    respx.mock.get(upstream.GAMES_URL).respond(200, text=games_csv())
    async with httpx.AsyncClient() as client:
        schedule = await upstream.fetch_schedule([2026], client=client)

    assert set(season for season, _team in schedule.by_team_season) == {2026}


@respx.mock
async def test_a_game_row_becomes_one_ref_per_CLUB():
    """Tenure is a property of a player's club, so the home side's list and the
    away side's list are needed separately."""
    respx.mock.get(upstream.GAMES_URL).respond(200, text=games_csv(seasons=[2026]))
    async with httpx.AsyncClient() as client:
        schedule = await upstream.fetch_schedule([2026], client=client)

    assert len(schedule.games(2026, "SEA")) == 6
    assert len(schedule.games(2026, "BUF")) == 6


@respx.mock
async def test_the_player_table_drops_positions_this_collector_cannot_place():
    """A position that cannot go on an availability cohort must not reach one,
    where it would be ranked against quarterbacks."""
    respx.mock.get(upstream.PLAYERS_URL).respond(200, text=players_csv())
    async with httpx.AsyncClient() as client:
        rows = await upstream.fetch_players(SEASON, client=client, span=3)

    assert {row.position for row in rows} <= {"QB", "RB", "WR", "TE", "K"}
    assert "00-0008888" not in {row.gsis_id for row in rows}  # the DT
    assert "00-0009999" not in {row.gsis_id for row in rows}  # last_season 1999


@respx.mock
async def test_designations_are_filtered_to_the_resolved_scope_as_they_stream():
    """~6,200 rows a season in, a few hundred out. Materialising the rest first is
    the `roster-scope` OOM in miniature."""
    for season in HISTORY_SEASONS:
        respx.mock.get(upstream.INJURIES_URL.format(season=season)).respond(
            200, text=injuries_csv(season)
        )
    async with httpx.AsyncClient() as client:
        result = await upstream.fetch_designations(
            HISTORY_SEASONS, client=client, keep_gsis=frozenset({"00-0000002"})
        )

    assert set(result.by_player) == {"00-0000002"}
    assert result.seasons_read == tuple(HISTORY_SEASONS)


@respx.mock
async def test_a_missing_season_is_recorded_rather_than_skipped():
    """A history silently missing a season is a well-formed number that is simply
    wrong — the same class of failure as an absence with an invented reason."""
    respx.mock.get(upstream.INJURIES_URL.format(season=2024)).respond(503)
    for season in (2025, 2026):
        respx.mock.get(upstream.INJURIES_URL.format(season=season)).respond(
            200, text=injuries_csv(season)
        )
    async with httpx.AsyncClient() as client:
        result = await upstream.fetch_designations(
            HISTORY_SEASONS, client=client, keep_gsis=frozenset({"00-0000002"})
        )

    assert result.seasons_missing == (2024,)
    assert result.seasons_read == (2025, 2026)


@respx.mock
async def test_participation_carries_the_club_so_a_healthy_player_has_a_tenure():
    """The half that lets a player who never reaches an injury report be
    enumerated at all: with no designation there is no other row to read a club
    off, and his whole season would otherwise contribute zero games possible."""
    respx.mock.get(upstream.SNAP_COUNTS_URL.format(season=2026)).respond(
        200, text=snap_counts_csv(2026)
    )
    async with httpx.AsyncClient() as client:
        result = await upstream.fetch_participation(
            [2026], client=client, keep_pfr=frozenset({"AlphQb00"})
        )

    assert result.snap_pct[("AlphQb00", 2026, 1)] == pytest.approx(0.80)
    assert result.team_of[("AlphQb00", 2026, 1)] == "SEA"


@respx.mock
async def test_schema_drift_fails_the_fetch_loudly():
    """An upstream that renames a column must fail with `reason=malformed` rather
    than map nulls into an append-only lake nobody rewrites."""
    respx.mock.get(upstream.PLAYERS_URL).respond(200, text="a,b,c\n1,2,3\n")
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await upstream.fetch_players(SEASON, client=client)


def test_positions_match_the_set_roster_scope_mints_its_scope_from():
    """A position this collector recognised and roster-scope did not could never
    be in scope anyway; one roster-scope recognised and this did not would be a
    silent hole. There is no import across the service boundary to enforce it."""
    for alias, canonical in {
        "QB": "QB", "HB": "RB", "TB": "RB", "SE": "WR", "SLOT": "WR",
        "Y": "TE", "PK": "K",
    }.items():  # fmt: skip
        assert normalize_position(alias) == canonical
    assert normalize_position("DT") is None
    assert normalize_position("") is None


# ── cost ─────────────────────────────────────────────────────────────────────


@respx.mock
async def test_a_second_fetch_sends_if_none_match_and_a_304_costs_no_reparse():
    """Both halves of conditional GET, together. Without the `etag_key` half
    nothing is saved — every poll still round-trips the full body — and without
    the memo half a `304` serves nothing.

    This is what makes a 43.8 MB cold window affordable: every pass after the
    first is a handful of `304`s carrying no bytes.
    """
    route = respx.mock.get(upstream.PLAYERS_URL)
    route.side_effect = [
        httpx.Response(200, text=players_csv(), headers={"ETag": '"v1"'}),
        httpx.Response(304),
    ]

    async with httpx.AsyncClient() as client:
        first = await upstream.fetch_players(SEASON, client=client, span=3)
        second = await upstream.fetch_players(SEASON, client=client, span=3)

    assert route.call_count == 2
    assert route.calls[1].request.headers.get("if-none-match") == '"v1"'
    assert first == second, "the memo did not serve the 304"


@respx.mock
async def test_a_304_with_no_memo_behind_it_re_reads_rather_than_serving_nothing():
    """The one inconsistent state worth defending: an ETag stored with no memo.
    It cannot arise from this module, but it can arise from a test that clears one
    and not the other. Serving nothing would be a silent, permanent empty."""
    route = respx.mock.get(upstream.PLAYERS_URL)
    route.side_effect = [
        httpx.Response(200, text=players_csv(), headers={"ETag": '"v1"'}),
        httpx.Response(304),
        httpx.Response(200, text=players_csv(), headers={"ETag": '"v1"'}),
    ]

    async with httpx.AsyncClient() as client:
        await upstream.fetch_players(SEASON, client=client, span=3)
        upstream._PLAYERS_MEMO.clear()  # the memo without the ETag
        rows = await upstream.fetch_players(SEASON, client=client, span=3)

    assert rows, "a 304 with no memo served nothing"
    assert route.call_count == 3


@respx.mock
async def test_the_etag_is_not_committed_when_the_body_dies_mid_stream():
    """An ETag claims you hold the WHOLE document. Committing one for a truncated
    read makes every later pass 304 against a partial document — a loud,
    self-retrying failure turned into a silent, sticky one."""
    from collector_core.conditional import ETAGS

    respx.mock.get(upstream.PLAYERS_URL).respond(200, text="not,a,players,table\n")
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await upstream.fetch_players(SEASON, client=client)

    assert ETAGS.get(upstream.PLAYERS_URL) is None

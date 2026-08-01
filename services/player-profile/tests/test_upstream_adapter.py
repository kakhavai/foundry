"""The adapters — parsing, filtering, and the conditional-GET memo.

Everything here is about the two rules that killed `roster-scope` on its first
deploy (never hold a document twice, filter as you parse) and about the memo
that makes a fifteen-season career sweep affordable.
"""

import httpx
import pytest
import respx

from player_profile.adapters import upstream
from player_profile.adapters.upstream import (
    FANTASY_POSITIONS,
    fetch_career_snaps,
    fetch_combine,
    fetch_profiles,
    normalize_position,
    source_ref,
)

from .conftest import SEASON, combine_csv, mock_upstreams, players_csv


async def _fetch_profiles():
    async with httpx.AsyncClient() as client:
        return await fetch_profiles(SEASON, client=client)


# ── position normalisation ───────────────────────────────────────────────────


def test_the_alias_table_matches_roster_scopes_offensive_subset():
    """A position this collector recognised and `roster-scope` did not could
    never be in scope; one `roster-scope` recognised and this did not would be a
    silent hole. There is no import across the service boundary to enforce it,
    so the pairs are pinned here."""
    assert normalize_position("HB") == "RB"
    assert normalize_position("TB") == "RB"
    assert normalize_position("SE") == "WR"
    assert normalize_position("FL") == "WR"
    assert normalize_position("SLOT") == "WR"
    assert normalize_position("Y") == "TE"
    assert normalize_position("PK") == "K"
    assert FANTASY_POSITIONS == frozenset({"QB", "RB", "WR", "TE", "K"})


def test_an_unrecognised_position_is_none_not_the_raw_string():
    """A position this collector cannot place on a curve must not reach
    `position_age_curve_stage`, which would bucket it against whichever default
    the table happened to carry."""
    assert normalize_position("DT") is None
    assert normalize_position("") is None


def test_normalisation_is_case_and_whitespace_insensitive():
    assert normalize_position(" qb ") == "QB"


# ── the profile table ────────────────────────────────────────────────────────


@respx.mock
async def test_only_recent_fantasy_position_players_are_kept():
    """25,029 rows in, 1,326 out against the live feed. Doing this after the
    fact is the `roster-scope` OOM in miniature."""
    mock_upstreams(respx.mock)

    table = await _fetch_profiles()

    gsis = {row.gsis_id for row in table.rows}
    assert "00-0009999" not in gsis, "the last_season recency filter is not applied"
    assert "00-0008888" not in gsis, "a defensive lineman was kept"
    assert len(table.rows) == 6
    assert all(row.position in FANTASY_POSITIONS for row in table.rows)


@respx.mock
async def test_the_draft_pick_column_is_read_as_the_overall_selection():
    """nflverse's `draft_pick` is the OVERALL number: round 5, pick 143 is the
    143rd player taken. Reading it as the within-round pick would give every
    late-round player a first-round pedigree score."""
    mock_upstreams(respx.mock)

    table = await _fetch_profiles()

    bravo = next(row for row in table.rows if row.gsis_id == "00-0000002")
    assert bravo.draft_round == 5
    assert bravo.draft_overall_pick == 143


@respx.mock
async def test_a_blank_numeric_cell_is_none_rather_than_zero():
    """Pick 0 would read as better than pick 1, and a zero draft year would sort
    ahead of every real one."""
    mock_upstreams(respx.mock)

    table = await _fetch_profiles()

    undrafted = next(row for row in table.rows if row.gsis_id == "00-0000004")
    assert undrafted.draft_year is None
    assert undrafted.draft_round is None
    assert undrafted.draft_overall_pick is None


@respx.mock
async def test_a_missing_column_fails_the_capture_loudly():
    """Schema drift must fail with `reason=malformed` rather than mapping nulls
    into an append-only lake that is never rewritten."""
    from collector_core.streaming import UpstreamSchemaError

    respx.mock.get(upstream.PLAYERS_URL).respond(200, text="gsis_id,position\na,QB\n")

    with pytest.raises(UpstreamSchemaError):
        await _fetch_profiles()


# ── the combine table ────────────────────────────────────────────────────────


@respx.mock
async def test_combine_is_keyed_by_pfr_id_because_it_carries_no_gsis_id():
    mock_upstreams(respx.mock)

    async with httpx.AsyncClient() as client:
        table = await fetch_combine(client=client)

    assert "BravRb00" in table.by_pfr_id
    assert table.by_pfr_id["BravRb00"].forty_yard == 4.45
    assert table.by_pfr_id["BravRb00"].bench_reps == 18


@respx.mock
async def test_an_absent_measurement_is_null_never_zero():
    """ "A zero forty time is numerically catastrophic downstream." The same
    applies to every other drill."""
    mock_upstreams(respx.mock)

    async with httpx.AsyncClient() as client:
        table = await fetch_combine(client=client)

    alpha = table.by_pfr_id["AlphQb00"]
    # `bench_reps` goes through `_int` and the timed/explosive drills through
    # `_float`, so asserting only on bench leaves every float field unpinned —
    # mutation testing caught exactly that. Both parsers are covered here.
    assert alpha.bench_reps is None
    assert alpha.forty_yard == 4.85

    charlie = table.by_pfr_id["Char Wr00"]
    assert charlie.broad_jump_inches is None
    assert charlie.three_cone is None
    assert charlie.shuttle is None
    assert charlie.vertical_inches == 36.0


@respx.mock
async def test_a_combine_row_without_a_join_key_is_skipped():
    mock_upstreams(
        respx.mock,
        combine=combine_csv(
            {
                "": {"forty": "4.4", "bench": "", "vertical": "", "broad_jump": "",
                     "cone": "", "shuttle": ""},
            }
        ),
    )  # fmt: skip

    async with httpx.AsyncClient() as client:
        table = await fetch_combine(client=client)

    assert table.by_pfr_id == {}


# ── career snaps ─────────────────────────────────────────────────────────────


@respx.mock
async def test_career_snaps_sum_across_seasons_and_across_rows():
    """Two half-rows per player per season, so last-write-wins would give half
    the real total and still look like a plausible career."""
    mock_upstreams(respx.mock)

    async with httpx.AsyncClient() as client:
        career = await fetch_career_snaps(SEASON, client=client)

    # 300 + 1100 + 1050
    assert career.total_for("AlphQb00") == 2450
    assert career.seasons_missing == ()


@respx.mock
async def test_a_player_with_no_join_key_gets_null_snaps_not_zero():
    """ "This player has no join key" and "this player took zero snaps" are
    different facts, and a zero flows straight into the load percentile as a
    genuine bottom-of-cohort reading."""
    mock_upstreams(respx.mock)

    async with httpx.AsyncClient() as client:
        career = await fetch_career_snaps(SEASON, client=client)

    assert career.total_for(None) is None
    assert career.total_for("") is None
    # A player WITH a key who simply never took a snap is a genuine zero.
    assert career.total_for("NeverPlayed00") == 0


@respx.mock
async def test_one_unreadable_season_is_recorded_rather_than_silently_skipped():
    """A career total silently missing three seasons is a well-formed number
    that is simply wrong — the same class of failure as a stale position."""
    mock_upstreams(respx.mock)
    respx.mock.get(upstream.SNAP_COUNTS_URL.format(season=2019)).mock(
        side_effect=httpx.ConnectError("gone")
    )

    async with httpx.AsyncClient() as client:
        career = await fetch_career_snaps(SEASON, client=client)

    assert career.seasons_missing == (2019,)


# ── the conditional-GET memo ─────────────────────────────────────────────────


@respx.mock
async def test_a_304_serves_the_memo_and_reports_that_nothing_was_republished():
    """Both halves. Serving the memo is what keeps the pass running; reporting
    `republished=False` is what lets the staleness guard measure an age rather
    than assume one."""
    mock_upstreams(respx.mock)

    first = await _fetch_profiles()
    assert first.republished is True

    respx.mock.get(upstream.PLAYERS_URL).respond(304)
    second = await _fetch_profiles()

    assert second.republished is False
    assert second.rows == first.rows
    assert len(second.rows) == 6


@respx.mock
async def test_a_conditional_request_carries_the_stored_etag():
    """Without the `If-None-Match` half nothing is saved: every poll still
    round-trips the whole 7.3 MB body."""
    route = respx.mock.get(upstream.PLAYERS_URL).respond(
        200, text=players_csv(), headers={"ETag": '"abc123"'}
    )
    mock_upstreams(respx.mock)
    respx.mock.get(upstream.PLAYERS_URL).respond(
        200, text=players_csv(), headers={"ETag": '"abc123"'}
    )

    await _fetch_profiles()
    await _fetch_profiles()

    last_request = route.calls[-1].request if route.calls else None
    assert last_request is not None
    assert last_request.headers.get("if-none-match") == '"abc123"'


@respx.mock
async def test_a_304_with_no_memo_behind_it_re_reads_rather_than_serving_nothing():
    """An inconsistent ETag store must self-heal at the cost of one download,
    not leave the collector serving an empty table."""
    upstream.ETAGS.set(upstream.PLAYERS_URL, '"stale"')
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(304)
        return httpx.Response(200, text=players_csv())

    respx.mock.get(upstream.PLAYERS_URL).mock(side_effect=handler)

    table = await _fetch_profiles()

    assert calls["n"] == 2
    assert len(table.rows) == 6


# ── source_ref ───────────────────────────────────────────────────────────────


def test_source_ref_names_all_three_upstreams():
    """The spec asks for the supplying upstream to be traceable so a corrected
    birth date can be chased. A profile row is a join across all three."""
    ref = source_ref(SEASON, 1)

    assert ref.count(",") == 2
    assert upstream.PLAYERS_URL in ref
    assert upstream.COMBINE_URL in ref
    assert f"snap_counts_{SEASON}.csv" in ref

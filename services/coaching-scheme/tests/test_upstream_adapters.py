"""The four adapters: parsing, filtering, attribution, and schema drift.

Each is tested against the **real wire format** — the full header, the
projected-away columns, the gzip framing — because a fixture narrowed to the
read columns cannot catch a projection bug and makes `required_columns`
vacuous.
"""

import gzip

import httpx
import pytest
import respx
from collector_core.conditional import ETagStore, UpstreamUnchanged
from collector_core.streaming import UpstreamSchemaError, UpstreamTruncated

from coaching_scheme.adapters import ftn as ftn_adapter
from coaching_scheme.adapters import games as games_adapter
from coaching_scheme.adapters import participation as participation_adapter
from coaching_scheme.adapters import pbp as pbp_adapter
from coaching_scheme.adapters.participation import classify
from coaching_scheme.adapters.pbp import is_neutral

from .conftest import (
    GAMES_HEADER,
    SEASON,
    ftn_document,
    games_document,
    participation_document,
    pbp_document,
    steady_coaches,
    team_week_plays,
)


async def fetch_games(document: str, *, season: int = SEASON, **kwargs):
    with respx.mock(assert_all_called=False) as router:
        router.get(games_adapter.UPSTREAM_URL).mock(
            return_value=httpx.Response(200, text=document)
        )
        async with httpx.AsyncClient() as client:
            return await games_adapter.fetch_team_week_coaches(
                season, client=client, **kwargs
            )


async def fetch_pbp(body: bytes, *, season: int = SEASON, **kwargs):
    with respx.mock(assert_all_called=False) as router:
        router.get(pbp_adapter.source_ref(season)).mock(
            return_value=httpx.Response(200, content=body)
        )
        async with httpx.AsyncClient() as client:
            return await pbp_adapter.fetch_weekly_buckets(
                season, client=client, **kwargs
            )


# --------------------------------------------------------------------------
# games.csv
# --------------------------------------------------------------------------


async def test_both_sides_of_a_game_yield_a_coach():
    """One row, two teams. Reading only the home side would halve the grid and
    silently give every away team a bye every week."""
    rows = await fetch_games(games_document(steady_coaches(weeks=1)))
    assert {row.team_id for row in rows} == {"AAA", "BBB", "CCC", "DDD"}


async def test_other_seasons_are_filtered_out_as_they_stream():
    """The file carries every season back to 1999. Materialising it to select
    one is the memory rule this library exists to enforce."""
    document = games_document(steady_coaches(weeks=1), season=2019)
    assert await fetch_games(document, season=SEASON) == []
    assert await fetch_games(document, season=2019)


async def test_postseason_rows_are_excluded():
    """A playoff game's coach is the same person, but admitting it extends a
    revision's span past the regular-season grid `effective_from_week` is
    defined on."""
    document = games_document(steady_coaches(weeks=1), game_type="POST")
    assert await fetch_games(document) == []


async def test_a_non_numeric_week_is_skipped_rather_than_crashing():
    rows_text = games_document(steady_coaches(weeks=1)).splitlines()
    broken = rows_text[1].split(",")
    broken[3] = "WC"
    document = "\n".join([rows_text[0], ",".join(broken), *rows_text[2:]]) + "\n"
    rows = await fetch_games(document)
    assert rows
    assert all(isinstance(row.week, int) for row in rows)


async def test_a_missing_column_fails_the_capture_loudly():
    """Schema drift must not map nulls into an append-only lake that is never
    rewritten."""
    header = GAMES_HEADER.replace(",home_coach", ",home_coach_renamed")
    with pytest.raises(UpstreamSchemaError):
        await fetch_games(header + "\n")


async def test_a_304_raises_upstream_unchanged_rather_than_an_http_error():
    """`raise_for_status()` gates on `is_success`, which is 2xx only — so a
    304 raises `HTTPStatusError` unless the conditional branch catches it
    first. Drop that branch and every unchanged upstream becomes a capture
    failure that writes `present: 0` over healthy data."""
    store = ETagStore()
    store.set(games_adapter.UPSTREAM_URL, '"v1"')
    with respx.mock(assert_all_called=False) as router:
        router.get(games_adapter.UPSTREAM_URL).mock(return_value=httpx.Response(304))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UpstreamUnchanged):
                await games_adapter.fetch_team_week_coaches(
                    SEASON, client=client, etag_store=store
                )


# --------------------------------------------------------------------------
# play-by-play
# --------------------------------------------------------------------------


async def test_weekly_buckets_are_keyed_by_team_and_week():
    plays = team_week_plays("AAA", 3, plays=10, passes=6, shotgun=4, no_huddle=2)
    buckets, index = await fetch_pbp(pbp_document(plays))

    bucket = buckets[("AAA", 3)]
    assert bucket.offensive_plays == 10
    assert bucket.shotgun_plays == 4
    assert bucket.no_huddle_plays == 2
    assert bucket.neutral_plays == 10
    assert bucket.neutral_passes == 6
    assert len(index) == 10


async def test_the_play_index_covers_offensive_plays_only():
    """`ftn` and `participation` both attribute through it, and both feeds mix
    in special-teams rows. An index carrying kicks would attribute a punt
    team's formation to the offense."""
    plays = team_week_plays("AAA", 1, plays=4)
    plays.append({**plays[0], "play_id": 99, "play_type": "punt"})
    _, index = await fetch_pbp(pbp_document(plays))
    assert 99 not in {play_id for _, play_id in index}


async def test_an_aborted_or_two_point_play_is_not_an_offensive_snap():
    plays = team_week_plays("AAA", 1, plays=2)
    plays.append({**plays[0], "play_id": 50, "aborted_play": 1})
    plays.append({**plays[0], "play_id": 51, "two_point_attempt": 1})
    buckets, _ = await fetch_pbp(pbp_document(plays))
    assert buckets[("AAA", 1)].offensive_plays == 2


async def test_postseason_plays_are_excluded():
    plays = [{**p, "season_type": "POST"} for p in team_week_plays("AAA", 1, plays=4)]
    buckets, _ = await fetch_pbp(pbp_document(plays))
    assert buckets == {}


async def test_a_blank_pass_oe_is_not_averaged_in_as_zero():
    """`None` rather than 0.0 on a missing numeric. Averaging a blank as zero
    drags a team's PROE toward the mean of the plays the model could not
    score, which is the changepoint series' own input."""
    plays = team_week_plays("AAA", 1, plays=4, pass_oe=10.0)
    plays[0] = {**plays[0], "pass_oe": "NA"}
    plays[1] = {**plays[1], "pass_oe": ""}
    buckets, _ = await fetch_pbp(pbp_document(plays))
    bucket = buckets[("AAA", 1)]
    assert bucket.proe_plays == 2
    assert bucket.proe_sum == pytest.approx(20.0)


async def test_a_truncated_gzip_body_raises_rather_than_yielding_half():
    """The framing property the plain-CSV feeds cannot have. Half a
    play-by-play would produce a full set of rates with every window silently
    halved — `present: 32`, ratio 1.0, and every number wrong."""
    body = pbp_document(team_week_plays("AAA", 1, plays=200))
    with pytest.raises(UpstreamTruncated):
        await fetch_pbp(body[: len(body) // 2])


async def test_a_non_gzip_body_is_a_loud_failure():
    """The adapter asks for the `.csv.gz` artifact. Being handed plain CSV is
    a wrong-URL bug and must not be parsed as though it were fine."""
    with respx.mock(assert_all_called=False) as router:
        router.get(pbp_adapter.source_ref(SEASON)).mock(
            return_value=httpx.Response(200, text="game_id,play_id\n1,1\n")
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(Exception):  # noqa: B017 — zlib.error, not ours
                await pbp_adapter.fetch_weekly_buckets(SEASON, client=client)


async def test_a_gzipped_document_round_trips_through_the_real_inflater():
    """The negative control for the two tests above: the happy path must work
    through the same code, or `UpstreamTruncated` would be indistinguishable
    from a broken adapter."""
    body = gzip.decompress(pbp_document(team_week_plays("AAA", 1, plays=3)))
    assert body.startswith(b"game_id,play_id")


def test_the_neutral_predicate_excludes_late_and_lopsided_plays():
    assert is_neutral(1, 0.5)
    assert is_neutral(3, 0.20)
    assert is_neutral(3, 0.80)
    assert not is_neutral(4, 0.5)
    assert not is_neutral(1, 0.19)
    assert not is_neutral(1, 0.81)


def test_a_play_with_no_win_probability_is_not_neutral():
    """Admitting it would put the league's least-documented plays into the
    statistic the changepoint test runs on."""
    assert not is_neutral(1, None)
    assert not is_neutral(None, 0.5)


async def test_the_weekly_proe_series_skips_weeks_with_no_scored_play():
    plays = team_week_plays("AAA", 1, plays=4, pass_oe=5.0)
    plays += [{**p, "pass_oe": "NA"} for p in team_week_plays("AAA", 2, plays=4)]
    buckets, _ = await fetch_pbp(pbp_document(plays))
    series = pbp_adapter.weekly_proe_series(buckets)
    assert series["AAA"] == [(1, pytest.approx(5.0))]


async def test_the_proe_series_is_week_ascending():
    """`detect` walks it in order; an unsorted series would compare
    non-adjacent weeks and report changepoints that never happened."""
    plays: list[dict] = []
    for week in (5, 1, 3):
        plays += team_week_plays("AAA", week, plays=2, pass_oe=float(week))
    buckets, _ = await fetch_pbp(pbp_document(plays))
    assert [week for week, _ in pbp_adapter.weekly_proe_series(buckets)["AAA"]] == [
        1,
        3,
        5,
    ]


# --------------------------------------------------------------------------
# ftn_charting and pbp_participation — attribution through the play index
# --------------------------------------------------------------------------


async def test_charting_rows_are_attributed_through_the_play_index():
    plays = team_week_plays("AAA", 2, plays=6)
    _, index = await fetch_pbp(pbp_document(plays))
    with respx.mock(assert_all_called=False) as router:
        router.get(ftn_adapter.source_ref(SEASON)).mock(
            return_value=httpx.Response(
                200, text=ftn_document(plays, motion_every=2, play_action_every=3)
            )
        )
        async with httpx.AsyncClient() as client:
            buckets = await ftn_adapter.fetch_charting_buckets(
                SEASON, client=client, play_index=index
            )
    bucket = buckets[("AAA", 2)]
    assert bucket.charted_plays == 6
    assert bucket.motion_plays == 3
    assert bucket.play_action_plays == 2


async def test_a_charted_play_absent_from_the_index_is_skipped():
    """This feed carries no possession team, so an unindexed row cannot be
    attributed at all. Guessing from the game id would give every
    special-teams snap to whichever team is named first."""
    plays = team_week_plays("AAA", 2, plays=4)
    with respx.mock(assert_all_called=False) as router:
        router.get(ftn_adapter.source_ref(SEASON)).mock(
            return_value=httpx.Response(200, text=ftn_document(plays))
        )
        async with httpx.AsyncClient() as client:
            buckets = await ftn_adapter.fetch_charting_buckets(
                SEASON, client=client, play_index={}
            )
    assert buckets == {}


async def test_participation_rows_are_attributed_and_classified():
    plays = team_week_plays("AAA", 4, plays=8)
    _, index = await fetch_pbp(pbp_document(plays))
    with respx.mock(assert_all_called=False) as router:
        router.get(participation_adapter.source_ref(SEASON)).mock(
            return_value=httpx.Response(200, text=participation_document(plays))
        )
        async with httpx.AsyncClient() as client:
            buckets = await participation_adapter.fetch_personnel_buckets(
                SEASON, client=client, play_index=index
            )
    bucket = buckets[("AAA", 4)]
    assert bucket.classified_plays == 8
    # Eight plays cycling four groupings: two of each.
    assert bucket.groupings == {"p11": 2, "p12": 2, "p21": 2, "p13": 2, "heavy": 0}


# --------------------------------------------------------------------------
# classify — the personnel parser
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("personnel", "expected"),
    [
        ("1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR", "p11"),
        ("1 C, 2 G, 1 QB, 1 RB, 2 T, 2 TE, 2 WR", "p12"),
        ("1 C, 2 G, 1 QB, 2 RB, 2 T, 1 TE, 2 WR", "p21"),
        ("1 C, 2 G, 1 QB, 1 RB, 2 T, 3 TE, 1 WR", "p13"),
        ("1 C, 2 G, 1 QB, 2 RB, 2 T, 2 TE, 1 WR", "heavy"),
        ("1 C, 2 G, 1 QB, 1 RB, 2 T, 4 TE", "heavy"),
        # 10 personnel: real, and in no named bucket. It must not be forced
        # into one — that would inflate whichever bucket was written last.
        ("1 C, 2 G, 1 QB, 1 RB, 2 T, 4 WR", None),
        ("1 C, 2 G, 1 QB, 2 RB, 2 T, 3 WR", None),
    ],
)
def test_classify_maps_the_named_groupings(personnel, expected):
    assert classify(personnel) == expected


def test_a_fullback_counts_toward_the_backfield():
    """21 personnel is routinely charted as one RB plus one FB."""
    assert classify("1 C, 2 G, 1 QB, 1 RB, 1 FB, 2 T, 1 TE, 1 WR") == "p21"


def test_a_grouping_with_no_quarterback_is_refused():
    """The signature of the special-teams rows this feed mixes in. Counting
    them as classified plays would deflate every rate by inflating the
    denominator."""
    assert classify("2 CB, 1 FB, 1 FS, 1 ILB, 2 OLB, 1 SS, 1 TE, 2 WR") is None


def test_an_empty_or_malformed_personnel_string_is_refused():
    assert classify("") is None
    assert classify("   ") is None
    assert classify("QB 1, RB 1") is None

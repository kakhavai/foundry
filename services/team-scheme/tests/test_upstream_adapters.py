"""The three adapters: parsing, filtering, attribution, and schema drift.

Each is tested against the **real wire format** — the full header, the
projected-away columns, the gzip framing — because a fixture narrowed to the
read columns cannot catch a projection bug and makes `required_columns`
vacuous.

Ported essentially intact from `coaching-scheme`, minus the `games.csv` coach
adapter and the weekly-PROE series that fed its changepoint detector. Both
belong to the deferred `coaching-staff` collector.
"""

import gzip

import httpx
import pytest
import respx
from collector_core.conditional import ETagStore, UpstreamUnchanged
from collector_core.streaming import UpstreamSchemaError, UpstreamTruncated

from team_scheme.adapters import ftn as ftn_adapter
from team_scheme.adapters import participation as participation_adapter
from team_scheme.adapters import pbp as pbp_adapter
from team_scheme.adapters.participation import classify
from team_scheme.adapters.pbp import is_neutral

from .conftest import (
    FTN_COLUMNS,
    PBP_COLUMNS,
    SEASON,
    ftn_document,
    participation_document,
    pbp_document,
    team_week_plays,
)

FTN_PLAY_ID_COLUMN = FTN_COLUMNS.index("nflverse_play_id")


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
    """The first line of defence for the guard's season-bounds arm: a playoff
    row carries a week number past the regular-season grid the profile is
    defined on."""
    plays = [{**p, "season_type": "POST"} for p in team_week_plays("AAA", 1, plays=4)]
    buckets, _ = await fetch_pbp(pbp_document(plays))
    assert buckets == {}


async def test_a_blank_pass_oe_is_not_averaged_in_as_zero():
    """`None` rather than 0.0 on a missing numeric. Averaging a blank as zero
    drags a team's PROE toward the mean of the plays the model could not
    score."""
    plays = team_week_plays("AAA", 1, plays=4, pass_oe=10.0)
    plays[0] = {**plays[0], "pass_oe": "NA"}
    plays[1] = {**plays[1], "pass_oe": ""}
    buckets, _ = await fetch_pbp(pbp_document(plays))
    bucket = buckets[("AAA", 1)]
    assert bucket.proe_plays == 2
    assert bucket.proe_sum == pytest.approx(20.0)


async def test_a_non_numeric_week_is_skipped_rather_than_crashing():
    """The feed carries the odd malformed row. Skipping it costs one week;
    crashing costs the pass, and `int()` on 'WC' is a `ValueError` that
    `fail_capture` would classify as an upstream outage."""
    plays = team_week_plays("AAA", 1, plays=2)
    plays.append({**plays[0], "play_id": 90, "week": "WC"})
    buckets, _ = await fetch_pbp(pbp_document(plays))
    assert set(buckets) == {("AAA", 1)}
    assert buckets[("AAA", 1)].offensive_plays == 2


async def test_a_row_with_no_possession_team_is_skipped():
    """Kickoffs, timeouts and end-of-quarter rows carry a blank `posteam`.
    Bucketing them under `""` would create a thirty-third 'team' that owes a
    profile and can never have one."""
    plays = team_week_plays("AAA", 1, plays=2)
    plays.append({**plays[0], "play_id": 91, "posteam": ""})
    buckets, _ = await fetch_pbp(pbp_document(plays))
    assert set(buckets) == {("AAA", 1)}


async def test_an_unparseable_numeric_cell_is_read_as_absent():
    """`_num` returns `None` for anything it cannot float, not 0.0. A `wp` of
    'inf?' must fail the neutral filter for being unknown rather than pass it
    for being small."""
    plays = team_week_plays("AAA", 1, plays=2, pass_oe=5.0)
    plays[0] = {**plays[0], "pass_oe": "not-a-number", "wp": "junk"}
    buckets, _ = await fetch_pbp(pbp_document(plays))
    bucket = buckets[("AAA", 1)]
    assert bucket.proe_plays == 1
    assert bucket.neutral_plays == 1


async def test_a_missing_column_fails_the_capture_loudly():
    """Schema drift must not map nulls into an append-only lake that is never
    rewritten. `required_columns` validates the FULL header even though
    `columns=` narrows the rows, so a rename is caught before any row is
    yielded."""
    header = ",".join(PBP_COLUMNS).replace(",pass_oe", ",pass_oe_renamed")
    body = gzip.compress((header + "\n").encode("utf-8"))
    with pytest.raises(UpstreamSchemaError):
        await fetch_pbp(body)


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


async def test_a_304_raises_upstream_unchanged_rather_than_an_http_error():
    """`raise_for_status()` gates on `is_success`, which is 2xx only — so a
    304 raises `HTTPStatusError` unless the conditional branch catches it
    first. Drop that branch and every unchanged upstream becomes a capture
    failure that writes `present: 0` over healthy data."""
    store = ETagStore()
    store.set(pbp_adapter.source_ref(SEASON), '"v1"')
    with respx.mock(assert_all_called=False) as router:
        router.get(pbp_adapter.source_ref(SEASON)).mock(
            return_value=httpx.Response(304)
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(UpstreamUnchanged):
                await pbp_adapter.fetch_weekly_buckets(
                    SEASON, client=client, etag_store=store
                )


def test_the_neutral_predicate_excludes_late_and_lopsided_plays():
    assert is_neutral(1, 0.5)
    assert is_neutral(3, 0.20)
    assert is_neutral(3, 0.80)
    assert not is_neutral(4, 0.5)
    assert not is_neutral(1, 0.19)
    assert not is_neutral(1, 0.81)


def test_a_play_with_no_win_probability_is_not_neutral():
    """Admitting it would put the league's least-documented plays into the
    collector's headline statistic — and `neutral_pass_rate` is also the
    coverage predicate, so it would move the ratio too."""
    assert not is_neutral(1, None)
    assert not is_neutral(None, 0.5)


async def test_a_fourth_down_punt_counts_as_a_decision_not_a_go():
    """The denominator is every fourth-down DECISION, not fourth-down snaps.
    Counting only attempts would report every team at 1.0."""
    plays = team_week_plays("AAA", 1, plays=1)
    plays.append(
        {**plays[0], "play_id": 60, "down": 4, "play_type": "punt", "punt_attempt": 1}
    )
    plays.append({**plays[0], "play_id": 61, "down": 4, "play_type": "pass"})
    buckets, _ = await fetch_pbp(pbp_document(plays))
    bucket = buckets[("AAA", 1)]
    assert bucket.fourth_down_decisions == 2
    assert bucket.fourth_down_gos == 1


async def test_a_clock_delta_across_a_drive_boundary_is_not_a_snap_interval():
    """`previous_clock` is keyed by `(game_id, drive)`. Dropping the drive from
    that key folds the change-of-possession gap into the pace statistic as
    though it were an inter-snap interval.

    **The gap has to be a short one.** A drive boundary spanning 400s is
    already refused by `MAX_PLAY_CLOCK_DELTA`, so a fixture built that way
    passes with or without the drive in the key and proves nothing — the bound
    does the work and the key is never exercised. 40s is both realistic (a
    punt and a return) and inside the bound, which is exactly the case only
    the drive key can catch.
    """
    first = team_week_plays("AAA", 1, plays=2, start_play_id=1)
    second = team_week_plays("AAA", 1, plays=2, start_play_id=10)
    for play in second:
        play["drive"] = 2
        play["game_seconds_remaining"] = int(play["game_seconds_remaining"]) - 40
    buckets, _ = await fetch_pbp(pbp_document(first + second))
    bucket = buckets[("AAA", 1)]
    # One usable 25s delta inside each drive. Without the drive in the key
    # there would be three samples and 90 seconds.
    assert bucket.neutral_clock_samples == 2
    assert bucket.neutral_clock_seconds == pytest.approx(50.0)


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


async def test_a_charted_row_with_a_malformed_play_id_is_skipped():
    """The id is the only join key either charted feed has. A row whose id
    will not parse cannot be attributed, and `int(float("NA"))` would end the
    pass over one bad row in a 47,316-row document."""
    plays = team_week_plays("AAA", 2, plays=4)
    _, index = await fetch_pbp(pbp_document(plays))
    document = ftn_document(plays).splitlines()
    broken = document[1].split(",")
    broken[FTN_PLAY_ID_COLUMN] = "NA"
    body = "\n".join([document[0], ",".join(broken), *document[2:]]) + "\n"
    with respx.mock(assert_all_called=False) as router:
        router.get(ftn_adapter.source_ref(SEASON)).mock(
            return_value=httpx.Response(200, text=body)
        )
        async with httpx.AsyncClient() as client:
            buckets = await ftn_adapter.fetch_charting_buckets(
                SEASON, client=client, play_index=index
            )
    assert buckets[("AAA", 2)].charted_plays == 3


async def test_an_unclassifiable_personnel_string_is_not_counted_at_all():
    """Skipped, not bucketed. Counting it as a classified play would inflate
    the denominator and silently deflate all five shares."""
    plays = team_week_plays("AAA", 4, plays=4)
    _, index = await fetch_pbp(pbp_document(plays))
    document = participation_document(plays).splitlines()
    broken = document[1].split(",")
    # The personnel column is the only quoted one; replace the whole row's
    # personnel with a special-teams grouping, which carries no quarterback.
    body = (
        "\n".join(
            [
                document[0],
                ",".join(broken[:5])
                + ',"2 CB, 1 FS, 4 LB, 1 SS, 3 DL","4 DL, 3 LB, 4 DB"',
                *document[2:],
            ]
        )
        + "\n"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(participation_adapter.source_ref(SEASON)).mock(
            return_value=httpx.Response(200, text=body)
        )
        async with httpx.AsyncClient() as client:
            buckets = await participation_adapter.fetch_personnel_buckets(
                SEASON, client=client, play_index=index
            )
    assert buckets[("AAA", 4)].classified_plays == 3


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
        # **13 personnel is 1 RB + 3 TE = 4, so a `heavy` check placed before
        # the explicit map swallows it and p13 becomes unreachable.** That bug
        # shipped once and this row is what caught it.
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


def test_thirteen_personnel_is_not_swallowed_by_the_heavy_threshold():
    """The ordering claim on its own, stated so a reader sees why the two
    parametrised rows above are not redundant: 13 personnel and 22 personnel
    both total 4 backs-plus-ends, and only the explicit map tells them apart.
    """
    from team_scheme.adapters.participation import HEAVY_THRESHOLD

    assert HEAVY_THRESHOLD == 4
    assert classify("1 C, 2 G, 1 QB, 1 RB, 2 T, 3 TE, 1 WR") == "p13"
    assert classify("1 C, 2 G, 1 QB, 2 RB, 2 T, 2 TE, 1 WR") == "heavy"


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

"""The four adapters: the wire format, the projections, and the join.

Each adapter is the only module that knows its feed's shape, so this is the
only place a wire-format assumption can be pinned. Everything here goes
through the real `stream_csv_dicts`, the real gzip inflater and the real
header validation.
"""

import gzip

import httpx
import pytest
import respx
from collector_core.streaming import UpstreamSchemaError, UpstreamTruncated

from defensive_front.adapters import injuries as injuries_adapter
from defensive_front.adapters import participation as participation_adapter
from defensive_front.adapters import pbp as pbp_adapter
from defensive_front.adapters import players as players_adapter
from defensive_front.capture import _fold

from . import season as season_module
from .conftest import SEASON, WEEK, Feeds, SpyLake, by_team, run_capture


async def _fetch(url: str, body: bytes, call, status: int = 200):
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(status, content=body))
        async with httpx.AsyncClient() as client:
            return await call(client)


# --------------------------------------------------------------------------
# play-by-play
# --------------------------------------------------------------------------


async def test_pbp_indexes_only_regular_season_dropbacks_in_the_window():
    built = season_module.build_season()
    fold = await _fetch(
        pbp_adapter.source_ref(SEASON),
        season_module.pbp_document(built),
        lambda c: pbp_adapter.fetch_pbp(SEASON, 3, client=c),
    )
    assert fold.weeks == {1, 2, 3}, "weeks past the requested one leaked in"
    assert all(drop.week <= 3 for drop in fold.dropbacks.values())
    # Runs are folded, not indexed: the pressure join has nothing to say
    # about them.
    assert len(fold.dropbacks) == sum(
        1 for play in built.plays if play.dropback and play.week <= 3
    )


async def test_pbp_drops_the_postseason():
    """The playoffs are charted in participation too, and folding them into a
    regular-season rating would silently extend the window."""
    built = season_module.build_season()
    fold = await _fetch(
        pbp_adapter.source_ref(SEASON),
        season_module.pbp_document(built, season_type="POST"),
        lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
    )
    assert fold.dropbacks == {}
    assert fold.defense == {}


async def test_pbp_excludes_two_point_attempts_from_carries():
    """Not scrimmage carries, and their yardage is not comparable."""
    built = season_module.build_season(weeks=[1])
    extra = [
        {
            "game_id": "2026_01_BBB_AAA",
            "play_id": "9001",
            "season_type": "REG",
            "week": "1",
            "posteam": "BBB",
            "defteam": "AAA",
            "play_type": "run",
            "qb_dropback": "0",
            "sack": "0",
            "rush_attempt": "1",
            "rushing_yards": "2",
            "two_point_attempt": "1",
        }
    ]
    with_two_point = await _fetch(
        pbp_adapter.source_ref(SEASON),
        season_module.pbp_document(built, extra_rows=extra),
        lambda c: pbp_adapter.fetch_pbp(SEASON, 1, client=c),
    )
    without = await _fetch(
        pbp_adapter.source_ref(SEASON),
        season_module.pbp_document(built),
        lambda c: pbp_adapter.fetch_pbp(SEASON, 1, client=c),
    )
    assert with_two_point.defense["AAA"].carries == without.defense["AAA"].carries


async def test_pbp_counts_a_stuff_at_or_behind_the_line():
    built = season_module.build_season(weeks=[1])
    fold = await _fetch(
        pbp_adapter.source_ref(SEASON),
        season_module.pbp_document(built),
        lambda c: pbp_adapter.fetch_pbp(SEASON, 1, client=c),
    )
    for team, line in fold.defense.items():
        expected = sum(
            1
            for play in built.plays
            if play.rush and play.defense == team and play.rushing_yards <= 0
        )
        assert line.stuffs == expected, team


async def test_a_missing_pbp_column_raises_rather_than_nulling_every_row():
    """`required_columns` validates the FULL header even though the rows are
    projected, so a rename upstream fails immediately instead of after a
    million rows have been mapped to blanks."""
    body = gzip.compress(b"game_id,play_id\n2026_01_BBB_AAA,1\n")
    with pytest.raises(UpstreamSchemaError):
        await _fetch(
            pbp_adapter.source_ref(SEASON),
            body,
            lambda c: pbp_adapter.fetch_pbp(SEASON, 1, client=c),
        )


async def test_a_truncated_gzip_body_raises():
    """The correctness property `gzipped=True` buys that the plain-CSV path
    cannot have: a short document is a PLAUSIBLE answer, so without the gzip
    trailer check it lands in the lake as a genuinely quiet week."""
    full = season_module.pbp_document(season_module.build_season())
    with pytest.raises(UpstreamTruncated):
        await _fetch(
            pbp_adapter.source_ref(SEASON),
            full[: len(full) // 2],
            lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
        )


async def test_a_404_propagates_rather_than_returning_an_empty_fold():
    """`play_by_play_<season>.csv.gz` does not exist until a season's first
    games are played — verified live, the 2026 artifact 404s today — so a 404
    is the normal offseason state and must become a `present: 0` envelope
    rather than a quiet week."""
    with pytest.raises(httpx.HTTPStatusError):
        await _fetch(
            pbp_adapter.source_ref(SEASON),
            b"",
            lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
            status=404,
        )


# --------------------------------------------------------------------------
# participation
# --------------------------------------------------------------------------


async def test_participation_keeps_only_charted_pass_rush_snaps():
    built = season_module.build_season()
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        season_module.participation_document(built),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    assert len(snaps) == sum(1 for play in built.plays if play.rushers > 0)
    assert all(snap.rushers > 0 for snap in snaps.values())


async def test_was_pressure_is_compared_not_truthy():
    """**The column's other value is the STRING `"FALSE"`, which is truthy.**
    A collector reading it for truthiness publishes a 100% pressure rate for
    every front in the league, with every field populated and plausible."""
    built = season_module.build_season()
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        season_module.participation_document(built),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    pressures = sum(1 for snap in snaps.values() if snap.was_pressure)
    assert 0 < pressures < len(snaps)


async def test_an_unthrown_dropback_carries_no_release_time():
    """42.8% populated on the real feed. `None`, not `0.0`: a sack has no
    release, and averaging it as zero would drag every mean toward zero."""
    built = season_module.build_season()
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        season_module.participation_document(built),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    charted = [snap for snap in snaps.values() if snap.time_to_throw is not None]
    assert 0 < len(charted) < len(snaps)
    assert all(snap.time_to_throw > 0 for snap in charted)


async def test_defender_ids_are_interned_across_the_document():
    """~22,000 rows x 11 defenders would otherwise mint 240,000 separate
    strings, and this is the collector's largest resident structure."""
    built = season_module.build_season()
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        season_module.participation_document(built),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    ids = [player for snap in snaps.values() for player in snap.defenders]
    assert len({id(player) for player in ids}) == len(set(ids))


async def test_participation_does_not_take_the_pbp_index():
    """**Structural, not stylistic.** `team-scheme` folds this feed against an
    index passed down from its play-by-play fetch, so a pass where
    play-by-play answers `304` folds it against an EMPTY index and publishes
    zero charted rows — and the unconditional re-fetch that follows repairs
    play-by-play, not the fold. Taking no dependency makes that unreachable."""
    import inspect

    signature = inspect.signature(participation_adapter.fetch_rush_snaps)
    assert set(signature.parameters) == {"season", "client", "etag_store"}


# --------------------------------------------------------------------------
# the join
# --------------------------------------------------------------------------


async def test_the_join_is_an_intersection():
    """A charted pass-rush snap that play-by-play does not call a dropback is
    dropped. On the real feed 5.24% of them are penalty-nullified `no_play`
    rows, which can carry a pressure but never a sack — counting them would
    deflate `pressure_to_sack_rate` by that much, silently."""
    built = season_module.build_season()
    extra = [
        {
            "nflverse_game_id": "2026_01_BBB_AAA",
            "play_id": "99001",
            "possession_team": "BBB",
            "number_of_pass_rushers": "4",
            "was_pressure": "TRUE",
            "time_to_throw": "2.50",
            "defense_players": ";".join(
                season_module.front_id("AAA", slot) for slot in range(7)
            ),
        }
    ]
    fold = await _fetch(
        pbp_adapter.source_ref(SEASON),
        season_module.pbp_document(built),
        lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
    )
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        season_module.participation_document(built, extra_rows=extra),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    assert ("2026_01_BBB_AAA", 99001) in snaps, "the fixture row was not parsed"

    baseline = _fold(
        await _fetch(
            pbp_adapter.source_ref(SEASON),
            season_module.pbp_document(built),
            lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
        ),
        {k: v for k, v in snaps.items() if k != ("2026_01_BBB_AAA", 99001)},
        {},
    )
    joined = _fold(fold, snaps, {})
    assert (
        joined.defense["AAA"].pass_rush_snaps == baseline.defense["AAA"].pass_rush_snaps
    ), "an unmatched charted snap reached the pressure denominator"


# --------------------------------------------------------------------------
# players
# --------------------------------------------------------------------------


async def test_players_keeps_only_the_front():
    """`DL` and `LB` against `DB` — the coarse cut the spec does NOT rule out.
    Filtered as it parses: on the real feed 25,036 rows in, 6,984 kept."""
    front = await _fetch(
        players_adapter.source_ref(),
        season_module.players_document(),
        lambda c: players_adapter.fetch_front_players(client=c),
    )
    assert len(front) == len(season_module.TEAMS) * season_module.FRONT_PER_TEAM
    assert all(
        reference.position in season_module.FRONT_POSITIONS
        for reference in front.values()
    )
    assert not any("S0" in gsis_id for gsis_id in front), (
        "a defensive back reached the front map"
    )


async def test_a_blank_jersey_number_is_none_not_zero():
    """`player-identity` scores `jersey_number` at 0.20 of its weighting, so a
    fabricated 0 is an active wrong signal rather than a missing one."""
    body = gzip.compress(
        (
            ",".join(season_module.PLAYERS_COLUMNS)
            + "\n00-0000001,A Player,DL,DE,AAA,,ACT,Somewhere\n"
        ).encode()
    )
    front = await _fetch(
        players_adapter.source_ref(),
        body,
        lambda c: players_adapter.fetch_front_players(client=c),
    )
    assert front["00-0000001"].jersey_number is None


# --------------------------------------------------------------------------
# injuries
# --------------------------------------------------------------------------


async def test_injuries_reads_the_upcoming_week_only():
    """The spec asks about the week ahead, not the one just played."""
    absences = await _fetch(
        injuries_adapter.source_ref(SEASON),
        season_module.injuries_document(week=WEEK + 1),
        lambda c: injuries_adapter.fetch_absences(SEASON, WEEK, client=c),
    )
    assert absences
    stale = await _fetch(
        injuries_adapter.source_ref(SEASON),
        season_module.injuries_document(week=WEEK),
        lambda c: injuries_adapter.fetch_absences(SEASON, WEEK, client=c),
    )
    assert stale == []


async def test_only_out_and_doubtful_count_as_absent():
    """`Questionable` is 1,281 rows of the real 2025 feed against 1,396 `Out`,
    and questionable players overwhelmingly play. The spec says out or
    doubtful."""
    absences = await _fetch(
        injuries_adapter.source_ref(SEASON),
        season_module.injuries_document(week=WEEK + 1),
        lambda c: injuries_adapter.fetch_absences(SEASON, WEEK, client=c),
    )
    assert {absence.status for absence in absences} == {"Out", "Doubtful"}


async def test_the_postseason_report_is_dropped():
    absences = await _fetch(
        injuries_adapter.source_ref(SEASON),
        season_module.injuries_document(week=WEEK + 1),
        lambda c: injuries_adapter.fetch_absences(SEASON, WEEK, client=c),
    )
    assert "DDD" not in {absence.team for absence in absences}


async def test_the_last_week_of_a_season_has_no_upcoming_week():
    """Correct rather than an omission: there is no game to be absent from."""
    absences = await _fetch(
        injuries_adapter.source_ref(SEASON),
        season_module.injuries_document(week=WEEK + 1),
        lambda c: injuries_adapter.fetch_absences(SEASON, 22, client=c),
    )
    assert absences == []


# --------------------------------------------------------------------------
# ...and what the join produces end to end
# --------------------------------------------------------------------------


async def test_front_continuity_uses_a_three_week_window_not_one():
    """**Behavioural, not a re-read of the constant.**

    `tests/season.py` replaces two of seven front starters in the LAST week
    only, for half the league. A one-week window would read those teams'
    current rotation as the new seven and score them near the floor; the
    three-week window still reads the established seven. On real 2025 data the
    same artifact put Green Bay at 0.118 on one week and 0.590 on three.
    """
    rows = by_team(await run_capture(Feeds(), lake=SpyLake()))
    stable = [
        rows[team]["front_continuity_index"]
        for team in season_module.TEAMS[: len(season_module.TEAMS) // 2]
    ]
    churned = [
        rows[team]["front_continuity_index"]
        for team in season_module.TEAMS[len(season_module.TEAMS) // 2 :]
    ]
    assert min(stable) > max(churned), (
        "the churned half is not distinguishable, so the window proves nothing"
    )
    assert min(churned) > 0.5, (
        "a one-week window would put these near the floor; three weeks should "
        f"not: {churned}"
    )

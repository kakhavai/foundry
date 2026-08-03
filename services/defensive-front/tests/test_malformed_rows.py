"""One bad row must not end a pass — and the arms coverage cannot see.

Two distinct concerns share this file because they share a failure shape.

**Malformed cells.** nflverse writes `NA`, empty strings and occasionally
nothing at all. Each adapter skips the row rather than raising, because a
season-long capture must not be lost to one unparseable cell. Every one of
those skips is a branch that a well-formed fixture never enters.

**Conditional expressions.** `docs/collectors.md` and this repo's own
experience both note that **100% branch coverage cannot see an unfixtured arm
of a conditional expression** — coverage.py emits no branch arc for a ternary,
so `x if cond else y` reports fully covered with `cond` only ever true. The
ternaries that decide a published value are therefore driven explicitly here,
by name, rather than left to the coverage number.
"""

import gzip

import httpx
import pytest
import respx

from defensive_front.adapters import injuries as injuries_adapter
from defensive_front.adapters import participation as participation_adapter
from defensive_front.adapters import pbp as pbp_adapter
from defensive_front.adapters import players as players_adapter
from defensive_front.adapters.identity import IdentityFailures, resolve_absences
from defensive_front.capture import _fold, _recent_weeks
from defensive_front.ratings import FRONT_WINDOW_WEEKS

from . import season as season_module
from .conftest import SEASON, WEEK, Feeds, SpyLake, by_team, run_capture


async def _fetch(url: str, body: bytes, call):
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, content=body))
        async with httpx.AsyncClient() as client:
            return await call(client)


def _pbp_body(rows: list[dict]) -> bytes:
    return gzip.compress(
        season_module._csv(season_module.PBP_COLUMNS, rows).encode("utf-8")
    )


def _pbp_row(**overrides) -> dict:
    row = {
        "game_id": "2026_01_BBB_AAA",
        "play_id": "1",
        "season_type": "REG",
        "week": "1",
        "posteam": "BBB",
        "defteam": "AAA",
        "play_type": "pass",
        "qb_dropback": "1",
        "sack": "0",
        "rush_attempt": "0",
        "rushing_yards": "",
        "two_point_attempt": "0",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# play-by-play
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"week": "NA"},  # a non-numeric week
        {"week": ""},
        {"play_id": ""},  # no play id to join on
        {"play_id": "not-a-number"},
        {"posteam": ""},  # no offence
        {"defteam": ""},  # no defence
        {"game_id": ""},
    ],
)
async def test_a_malformed_pbp_row_is_skipped_not_raised(bad):
    good = _pbp_row(play_id="2")
    fold = await _fetch(
        pbp_adapter.source_ref(SEASON),
        _pbp_body([_pbp_row(**bad), good]),
        lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
    )
    assert len(fold.dropbacks) == 1, "the good row was lost with the bad one"


@pytest.mark.parametrize("cell", ["NA", "", "not-a-number"])
async def test_an_unparseable_rushing_yardage_reads_as_zero(cell):
    """Zero rather than `None`: every column read through `_num` is a flag or
    a yardage, and a blank means the play did not have that thing. A run with
    no recorded yardage is a run for no gain, which is also a stuff."""
    fold = await _fetch(
        pbp_adapter.source_ref(SEASON),
        _pbp_body(
            [
                _pbp_row(
                    qb_dropback="0",
                    rush_attempt="1",
                    rushing_yards=cell,
                    play_type="run",
                )
            ]
        ),
        lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
    )
    line = fold.defense["AAA"]
    assert line.carries == 1
    assert line.line_yards == 0.0
    assert line.stuffs == 1


async def test_a_sack_without_a_dropback_flag_is_still_indexed():
    """A sack IS a dropback. The `or` is what keeps the index correct if a row
    ever carries one without the other, and this is the fixture that makes the
    second arm real rather than defensive."""
    fold = await _fetch(
        pbp_adapter.source_ref(SEASON),
        _pbp_body([_pbp_row(qb_dropback="0", sack="1")]),
        lambda c: pbp_adapter.fetch_pbp(SEASON, WEEK, client=c),
    )
    assert len(fold.dropbacks) == 1
    assert next(iter(fold.dropbacks.values())).sack is True


# --------------------------------------------------------------------------
# participation
# --------------------------------------------------------------------------


def _participation_body(rows: list[dict]) -> bytes:
    return season_module._csv(season_module.PARTICIPATION_COLUMNS, rows).encode("utf-8")


def _participation_row(**overrides) -> dict:
    row = {
        "nflverse_game_id": "2026_01_BBB_AAA",
        "play_id": "1",
        "possession_team": "BBB",
        "number_of_pass_rushers": "4",
        "was_pressure": "TRUE",
        "time_to_throw": "2.60",
        "defense_players": "00-0001;00-0002",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "bad",
    [
        {"number_of_pass_rushers": ""},  # not charted
        {"number_of_pass_rushers": "NA"},  # charted as unparseable
        {"number_of_pass_rushers": "0"},  # a run: no rush to describe
        {"play_id": ""},
        {"play_id": "junk"},
        {"nflverse_game_id": ""},
    ],
)
async def test_a_malformed_participation_row_is_skipped(bad):
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        _participation_body(
            [_participation_row(**bad), _participation_row(play_id="2")]
        ),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    assert set(snaps) == {("2026_01_BBB_AAA", 2)}


async def test_an_unparseable_release_time_is_none_not_zero():
    """Averaging an unparseable cell as `0.0` would drag every defence's mean
    faced release time toward zero — and that column is the timing guard's
    whole regressor."""
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        _participation_body([_participation_row(time_to_throw="NA")]),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    assert next(iter(snaps.values())).time_to_throw is None


async def test_blank_entries_in_the_defender_list_are_dropped():
    """A trailing semicolon would otherwise mint an empty-string player id
    that appears on every snap of the season."""
    snaps = await _fetch(
        participation_adapter.source_ref(SEASON),
        _participation_body([_participation_row(defense_players="00-0001;;00-0002;")]),
        lambda c: participation_adapter.fetch_rush_snaps(SEASON, client=c),
    )
    assert next(iter(snaps.values())).defenders == ("00-0001", "00-0002")


# --------------------------------------------------------------------------
# players and injuries
# --------------------------------------------------------------------------


async def test_a_roster_row_with_no_id_is_skipped():
    body = gzip.compress(
        season_module._csv(
            season_module.PLAYERS_COLUMNS,
            [
                {
                    "gsis_id": "",
                    "position_group": "DL",
                    "position": "DE",
                    "latest_team": "AAA",
                    "jersey_number": "91",
                },
                {
                    "gsis_id": "00-0000009",
                    "position_group": "LB",
                    "position": "OLB",
                    "latest_team": "AAA",
                    "jersey_number": "55",
                },
            ],
        ).encode()
    )
    front = await _fetch(
        players_adapter.source_ref(),
        body,
        lambda c: players_adapter.fetch_front_players(client=c),
    )
    assert set(front) == {"00-0000009"}


async def test_an_unparseable_jersey_number_is_none():
    body = gzip.compress(
        season_module._csv(
            season_module.PLAYERS_COLUMNS,
            [
                {
                    "gsis_id": "00-0000009",
                    "position_group": "DL",
                    "position": "DT",
                    "latest_team": "AAA",
                    "jersey_number": "NA",
                }
            ],
        ).encode()
    )
    front = await _fetch(
        players_adapter.source_ref(),
        body,
        lambda c: players_adapter.fetch_front_players(client=c),
    )
    assert front["00-0000009"].jersey_number is None


async def test_an_injury_row_with_no_id_or_team_is_skipped():
    body = gzip.compress(
        season_module._csv(
            season_module.INJURIES_COLUMNS,
            [
                {
                    "game_type": "REG",
                    "week": str(WEEK + 1),
                    "gsis_id": "",
                    "team": "AAA",
                    "report_status": "Out",
                },
                {
                    "game_type": "REG",
                    "week": str(WEEK + 1),
                    "gsis_id": "00-0000009",
                    "team": "",
                    "report_status": "Out",
                },
                {
                    "game_type": "REG",
                    "week": "NA",
                    "gsis_id": "00-0000009",
                    "team": "AAA",
                    "report_status": "Out",
                },
                {
                    "game_type": "REG",
                    "week": str(WEEK + 1),
                    "gsis_id": "00-0000010",
                    "team": "AAA",
                    "position": "DT",
                    "report_status": "Out",
                },
            ],
        ).encode()
    )
    absences = await _fetch(
        injuries_adapter.source_ref(SEASON),
        body,
        lambda c: injuries_adapter.fetch_absences(SEASON, WEEK, client=c),
    )
    assert [absence.gsis_id for absence in absences] == ["00-0000010"]


# --------------------------------------------------------------------------
# The conditional-expression arms
# --------------------------------------------------------------------------


async def test_an_absent_player_outside_the_front_map_is_never_queried():
    """The `if absence.gsis_id in front` arm. With no matching front player
    there is nothing to resolve, and the client must not be called at all —
    the empty-query early return."""
    calls: list = []

    class Never:
        failures: dict = {}

        async def resolve_many(self, queries):
            calls.append(queries)
            return {}

    resolved, unresolved = await resolve_absences(
        [
            injuries_adapter.Absence(
                gsis_id="00-not-a-front-player",
                team="AAA",
                position="CB",
                status="Out",
            )
        ],
        season=SEASON,
        front={},
        identity=Never(),
        failures=IdentityFailures(),
    )
    assert (resolved, unresolved) == ({}, 0)
    assert calls == []


def test_the_recent_window_takes_the_last_weeks_actually_sampled():
    """Counted back from the requested week, a bye or an unplayed week would
    silently shrink the window."""
    assert _recent_weeks({1, 2, 3, 4, 5}) == frozenset({3, 4, 5})
    assert len(_recent_weeks({1, 2, 3, 4, 5})) == FRONT_WINDOW_WEEKS
    # Fewer sampled weeks than the window: take what there is rather than
    # index off the front of the list.
    assert _recent_weeks({2, 7}) == frozenset({2, 7})
    assert _recent_weeks(set()) == frozenset()


def test_the_fold_records_no_front_snaps_without_a_roster_map():
    """The `if not front: continue` arm. Counting the secondary as part of the
    front would make continuity a number about the wrong eleven players."""
    fold = pbp_adapter.PbpFold()
    fold.dropbacks[("g", 1)] = pbp_adapter.Dropback(
        defense="AAA", offense="BBB", week=1, game_id="g", sack=False
    )
    fold.opponents[("AAA", "g")] = "BBB"
    fold.weeks.add(1)
    snap = participation_adapter.RushSnap(
        rushers=4, was_pressure=True, time_to_throw=2.5, defenders=("00-0001",)
    )
    totals = _fold(fold, {("g", 1): snap}, {})
    assert totals.front_snaps == {}
    assert totals.recent_front_snaps == {}
    # ...while every other counter still moved.
    assert totals.defense["AAA"].pressures == 1


async def test_one_rateable_team_reports_zero_adjusted_variance():
    """The `if len(values) > 1 else 0.0` arm — a **ternary**, which branch
    coverage cannot see an unfixtured arm of, so it is driven by name.

    Only one defence keeps its charted snaps, so only one row carries an
    adjusted rate. `statistics.pvariance` RAISES on a one-element sample, so
    this arm is the difference between a gauge reading zero and a capture that
    dies after every upstream has already succeeded.
    """
    solo = season_module.TEAMS[0]
    built = season_module.build_season()
    kept = [play for play in built.plays if play.defense == solo or play.rush]
    feeds = Feeds(
        bodies={
            "participation": season_module.participation_document(
                season_module.Season(plays=kept, season=built.season)
            )
        }
    )
    envelopes = await run_capture(feeds, lake=SpyLake())
    rows = by_team(envelopes)
    rateable = [
        team
        for team, row in rows.items()
        if row["pressure_rate_generated_adj"] is not None
    ]
    assert rateable == [solo], rateable
    assert envelopes["defensive_front_strength"].coverage.present == 1


def test_a_tie_in_the_recent_window_does_not_move_the_index():
    """The `(-snaps, id)` sort key, on a fixture where the tie MATTERS.

    An earlier version of this test gave every window player the same snap
    count, which made any seven of them sum identically and left the mutation
    that drops the id tiebreak alive. Here the eighth and ninth rotational
    players are tied in the recent window but carry very different window
    totals, so which of them enters the rotation changes the published number
    — and only the id tiebreak makes that deterministic between two passes
    that streamed their games in different orders.
    """
    window = {f"p{i}": 100 for i in range(7)}
    window["tie-a"] = 400
    window["tie-b"] = 4
    forward = {f"p{i}": 9 for i in range(6)}
    forward["tie-a"] = 5
    forward["tie-b"] = 5
    backward = dict(reversed(list(forward.items())))

    from defensive_front.ratings import continuity_index

    first = continuity_index(window, forward)
    second = continuity_index(window, backward)
    assert first == second, (first, second)
    # ...and the tie is load-bearing: whichever of the two is chosen changes
    # the answer, so this is not a degenerate fixture. Break the tie the other
    # way and the index moves by a third.
    only_b = continuity_index(window, {**forward, "tie-a": 1})
    assert only_b is not None and abs(only_b - first) > 0.3

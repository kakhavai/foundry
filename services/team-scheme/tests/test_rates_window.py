"""The window is one team's regular season — three arms, and the removal.

This file replaces `coaching-scheme`'s `test_guard_window.py`. The guard there
asked "does this rate window straddle a **staff revision** boundary"; there are
no revisions here, so the question changes to "is this rate window exactly one
team's regular season". **Three arms, and they need separate fixtures** because
they fail identically from the outside — a refused row and a coverage entry —
while catching different bugs:

* **Arm A, a foreign team's bucket in the fold.** The direct form, and the one
  that enforces the *removed coupling's replacement*: a fold keyed to anything
  wider than the team dies here.
* **Arm B, a sampled week outside the regular season.** A postseason row past
  the adapter's `season_type` filter.
* **Arm C, `games_sampled` exceeding the weeks sampled.** A bucket that
  accumulated two games under one `(team, week)` key leaves every bucket
  correctly attributed to the right team and the right week while making the
  sample larger than the window. Arms A and B pass it.

The last section is the part this collector needs and `coaching-scheme` did
not: **tests that fail if the staff coupling comes back.** They are not
paranoia. The staff half is a written, reviewed, still-reachable branch, and
"restore the revision keying now that we have a coach feed" is a one-commit
change whose failure mode is a confident false attribution rather than an
error.
"""

import pytest

from team_scheme import rates as rates_module
from team_scheme.adapters.pbp import WeeklyBucket
from team_scheme.rates import (
    REASON_FOREIGN_TEAM_IN_WINDOW,
    REASON_GAMES_EXCEED_WEEKS,
    REASON_WEEK_OUTSIDE_SEASON,
    REGULAR_SEASON_MAX_WEEK,
    TeamSeasonRates,
    WindowIsNotTheTeamSeason,
    aggregate,
    assert_window_is_the_team_season,
    weeks_in_team_season,
)

from .conftest import (
    LATER,
    Feeds,
    SpyLake,
    proe_with_shift,
    run_capture,
)


def rates(
    *,
    games_sampled: int = 3,
    sampled_weeks: tuple[int, ...] = (1, 2, 3),
    folded_teams: frozenset[str] = frozenset({"AAA"}),
) -> TeamSeasonRates:
    """A `TeamSeasonRates` with only the guard's inputs set.

    The rate fields are irrelevant to the guard by construction — it inspects
    provenance, never a number — and passing them would obscure that.
    """
    return TeamSeasonRates(
        games_sampled=games_sampled,
        sampled_weeks=sampled_weeks,
        folded_teams=folded_teams,
        neutral_pass_rate=0.5,
        pass_rate_over_expected=1.0,
        sec_per_play_neutral=25.0,
        no_huddle_rate=0.1,
        shotgun_rate=0.5,
        play_action_rate=0.2,
        pre_snap_motion_rate=0.5,
        personnel_rates=None,
        fourth_down_go_rate=0.0,
    )


# --------------------------------------------------------------------------
# Arm A — a foreign team in the window
# --------------------------------------------------------------------------


def test_a_window_of_exactly_one_team_season_is_accepted():
    """The negative control. Without it, a guard that raised unconditionally
    would pass every other test in this file."""
    assert assert_window_is_the_team_season("AAA", rates()) is None


def test_another_teams_bucket_in_the_fold_is_refused():
    """**The arm that enforces the removal.** A profile claiming to describe
    AAA whose sample came partly from BBB is not a degraded measurement, it is
    a measurement of a thing that does not exist."""
    with pytest.raises(WindowIsNotTheTeamSeason) as caught:
        assert_window_is_the_team_season(
            "AAA", rates(folded_teams=frozenset({"AAA", "BBB"}))
        )
    assert caught.value.reason == REASON_FOREIGN_TEAM_IN_WINDOW
    assert "BBB" in caught.value.detail


def test_a_window_made_entirely_of_the_wrong_team_is_refused():
    """The mirror. A guard checking `team_id in folded_teams` rather than
    `folded_teams <= {team_id}` accepts a mixed fold and only catches a fully
    swapped one; this pins the direction the subset check gets wrong."""
    with pytest.raises(WindowIsNotTheTeamSeason) as caught:
        assert_window_is_the_team_season("AAA", rates(folded_teams=frozenset({"BBB"})))
    assert caught.value.reason == REASON_FOREIGN_TEAM_IN_WINDOW


# --------------------------------------------------------------------------
# Arm B — a week outside the regular season
# --------------------------------------------------------------------------


def test_a_postseason_week_is_refused():
    with pytest.raises(WindowIsNotTheTeamSeason) as caught:
        assert_window_is_the_team_season("AAA", rates(sampled_weeks=(1, 2, 19)))
    assert caught.value.reason == REASON_WEEK_OUTSIDE_SEASON
    assert "19" in caught.value.detail


def test_week_zero_is_refused():
    """The lower bound. A guard checking only the ceiling passes this."""
    with pytest.raises(WindowIsNotTheTeamSeason) as caught:
        assert_window_is_the_team_season(
            "AAA", rates(sampled_weeks=(0, 1), games_sampled=2)
        )
    assert caught.value.reason == REASON_WEEK_OUTSIDE_SEASON


def test_the_last_regular_season_week_is_accepted():
    """The boundary, asserted against the literal 18 rather than against the
    constant, so shrinking the constant to 17 is observable. An 18-week season
    is the current format; a 17-week one simply never produces a week 18."""
    assert REGULAR_SEASON_MAX_WEEK == 18
    assert (
        assert_window_is_the_team_season(
            "AAA", rates(sampled_weeks=(17, 18), games_sampled=2)
        )
        is None
    )


# --------------------------------------------------------------------------
# Arm C — games_sampled exceeding the weeks folded
# --------------------------------------------------------------------------


def test_more_games_than_weeks_is_refused_even_when_every_bucket_is_right():
    """A duplicate fold. **Arms A and B cannot see this**: every bucket
    belongs to the right team and every week is in the season, so the only
    thing wrong is that four games came out of three weeks — which is
    impossible, because a team plays at most one a week."""
    with pytest.raises(WindowIsNotTheTeamSeason) as caught:
        assert_window_is_the_team_season("AAA", rates(games_sampled=4))
    assert caught.value.reason == REASON_GAMES_EXCEED_WEEKS


def test_exactly_as_many_games_as_weeks_is_accepted():
    """The boundary. `>` rather than `>=` — off by one here would refuse every
    team that played every week it was folded over, which is most of them."""
    assert assert_window_is_the_team_season("AAA", rates(games_sampled=3)) is None


def test_a_bye_week_lowers_the_ceiling_and_that_is_correct():
    """The ceiling is `sampled_weeks`, deliberately the tighter of the two
    candidates.

    A snapless week contributes no game either — `bucket.games` is only added
    to inside the offensive-snap branch — so a team folded over four weeks
    with a bye in one of them has at most three games, and the tighter ceiling
    admits it. A wider "weeks folded" ceiling was tried first and could not be
    distinguished from this one by any input the adapter can produce, which is
    what an unfalsifiable distinction looks like from the mutation harness.
    """
    assert (
        assert_window_is_the_team_season(
            "AAA", rates(games_sampled=3, sampled_weeks=(1, 2, 4))
        )
        is None
    )
    with pytest.raises(WindowIsNotTheTeamSeason):
        assert_window_is_the_team_season(
            "AAA", rates(games_sampled=4, sampled_weeks=(1, 2, 4))
        )


def test_an_empty_window_is_clean():
    """A team that has not played has nothing to blend. Refusing it would turn
    'this team has not played' into an error; `capture` records it missing
    with `no_games_sampled_for_this_team` instead, which is more accurate."""
    empty = assert_window_is_the_team_season(
        "AAA", rates(games_sampled=0, sampled_weeks=())
    )
    assert empty is None


# --------------------------------------------------------------------------
# `weeks_in_team_season` — the selection the guard checks
# --------------------------------------------------------------------------


def _buckets(*keys: tuple[str, int]) -> dict[tuple[str, int], WeeklyBucket]:
    out: dict[tuple[str, int], WeeklyBucket] = {}
    for team, week in keys:
        bucket = WeeklyBucket(team_id=team, week=week)
        bucket.offensive_plays = 10
        bucket.games.add(f"{team}-{week}")
        bucket.neutral_plays = 10
        bucket.neutral_passes = 5
        out[(team, week)] = bucket
    return out


def test_weeks_in_team_season_selects_only_this_teams_weeks():
    """**The removal, at the selection.** The one place a team-season becomes
    a set of weeks. A selection that ignored the team would hand `aggregate`
    every team's buckets and produce a league-average profile 32 times."""
    buckets = _buckets(("AAA", 1), ("AAA", 3), ("BBB", 2), ("BBB", 5))
    assert weeks_in_team_season("AAA", buckets) == (1, 3)
    assert weeks_in_team_season("BBB", buckets) == (2, 5)


def test_weeks_in_team_season_is_ascending_regardless_of_insertion_order():
    """`aggregate` folds in this order and `sampled_weeks` is published from
    it, so an unsorted return would make the digest depend on dict iteration
    order and defeat the unchanged-snapshot gate."""
    buckets = _buckets(("AAA", 4), ("AAA", 1), ("AAA", 3))
    assert weeks_in_team_season("AAA", buckets) == (1, 3, 4)


def test_weeks_in_team_season_is_empty_for_a_team_that_never_appears():
    assert weeks_in_team_season("ZZZ", _buckets(("AAA", 1))) == ()


def test_sampled_weeks_excludes_weeks_with_no_offensive_snaps():
    """`sampled_weeks` is what arm B inspects and what a consumer verifies the
    window from, so widening what feeds it weakens both silently — a bye would
    be reported as part of the rate window.

    Removing the `offensive_plays > 0` filter survived the whole
    `coaching-scheme` suite before its equivalent test existed.
    """
    buckets = {}
    for week in (1, 2, 3, 4):
        bucket = WeeklyBucket(team_id="AAA", week=week)
        if week in (1, 2):
            bucket.offensive_plays = 10
            bucket.games.add(f"g{week}")
            bucket.neutral_plays = 10
            bucket.neutral_passes = 5
        buckets[("AAA", week)] = bucket

    profile = aggregate(
        "AAA",
        [1, 2, 3, 4],
        play_buckets=buckets,
        charting_buckets=None,
        personnel_buckets=None,
    )
    assert profile.sampled_weeks == (1, 2)
    assert profile.games_sampled == 2


def test_a_bucket_keyed_to_one_team_but_carrying_another_is_refused():
    """**The test that makes arm A more than a tautology.**

    `folded_teams` must be read from the buckets, never rebuilt from
    `aggregate`'s own `team_id` argument. Rebuilt from the argument the set is
    `{team_id}` by construction, arm A can never fire, and the mutation is
    invisible: the selection is correct, so the honest answer and the
    tautological one agree on every input the adapter produces.

    They disagree on a **mis-keyed bucket** — one filed under `("AAA", 1)`
    while carrying `team_id="BBB"`. That is the real corruption arm A is for:
    `pbp.py` writes `buckets[(team, week)] = WeeklyBucket(team_id=team, ...)`,
    and any change that lets those two drift produces exactly this. Read from
    the buckets it is caught; rebuilt from the argument it publishes BBB's
    snaps as AAA's profile with nothing to say so.
    """
    bucket = WeeklyBucket(team_id="BBB", week=1)
    bucket.offensive_plays = 10
    bucket.games.add("g1")
    bucket.neutral_plays = 10
    bucket.neutral_passes = 5

    profile = aggregate(
        "AAA",
        [1],
        play_buckets={("AAA", 1): bucket},
        charting_buckets=None,
        personnel_buckets=None,
    )
    assert profile.folded_teams == frozenset({"BBB"})
    with pytest.raises(WindowIsNotTheTeamSeason) as caught:
        assert_window_is_the_team_season("AAA", profile)
    assert caught.value.reason == REASON_FOREIGN_TEAM_IN_WINDOW


def test_aggregate_reports_the_teams_it_actually_folded():
    """The happy-path half of the claim above: on a correct fold the
    provenance names exactly the team asked for, so the test above is
    demonstrating a refusal rather than a guard that refuses everything."""
    buckets = _buckets(("AAA", 1), ("BBB", 1))
    profile = aggregate(
        "BBB",
        [1],
        play_buckets=buckets,
        charting_buckets=None,
        personnel_buckets=None,
    )
    assert profile.folded_teams == frozenset({"BBB"})


# --------------------------------------------------------------------------
# End to end — the window as `capture` actually applies it
# --------------------------------------------------------------------------


async def test_each_teams_profile_is_folded_from_its_own_season(lake: SpyLake):
    """The whole point, through the real pipeline.

    Four teams, twelve weeks each. Every profile must cover exactly its own
    team's weeks, and no two profiles may share a game. An `aggregate` that
    folded by week range or by season alone fails this on both counts.
    """
    envelopes = await run_capture(lake=lake)
    rows = envelopes["team_scheme_profile"].signals
    assert len(rows) == 4, rows

    for row in rows:
        # Paired with the length assertion above: `all([])` is True, so a row
        # set that silently became empty would otherwise pass this.
        assert row["sampled_weeks"], row
        assert row["sampled_weeks"] == list(range(1, 13))
        assert row["games_sampled"] == 12


async def test_one_teams_rate_shift_does_not_move_another_teams_profile(
    lake: SpyLake,
):
    """**The blend made visible as a number, and the cross-team leak with it.**

    AAA's PROE is +1.0 through week 6 and +21.0 from week 7, so its season
    profile is the honest blend at +11.0 — a true statement about the season
    and a poor predictor of next week, exactly as the README says. Every other
    team stays at +1.0.

    A fold that reached past the team would drag all four toward +6.0. A fold
    keyed to a week range would split AAA into two rows. Both are refused, and
    the numbers say which.
    """
    envelopes = await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=20.0)), lake=lake
    )
    by_team = {row["team_id"]: row for row in envelopes["team_scheme_profile"].signals}
    assert len(by_team) == 4
    # One row for AAA, not two: there is no regime boundary to split on.
    assert by_team["AAA"]["pass_rate_over_expected"] == pytest.approx(11.0)
    for team in ("BBB", "CCC", "DDD"):
        assert by_team[team]["pass_rate_over_expected"] == pytest.approx(1.0)


async def test_a_refused_window_costs_coverage_and_is_named(lake: SpyLake, monkeypatch):
    """A refused window must not publish. Forced by making the guard refuse
    the first team it sees — the collector's own structure makes a genuine
    foreign fold nearly unrepresentable, which is the design working, so the
    guard has to be provoked to prove it is wired in at all."""
    real = rates_module.assert_window_is_the_team_season
    seen: list[str] = []

    def refuse_first(team_id, profile):
        if not seen:
            seen.append(team_id)
            raise WindowIsNotTheTeamSeason(
                REASON_FOREIGN_TEAM_IN_WINDOW, "forced for this test"
            )
        return real(team_id, profile)

    monkeypatch.setattr(rates_module, "assert_window_is_the_team_season", refuse_first)

    envelopes = await run_capture(lake=lake, now=LATER)
    profile = envelopes["team_scheme_profile"]
    published = {row["team_id"] for row in profile.signals}

    assert seen, "the guard was never called"
    assert seen[0] not in published
    assert seen[0] in profile.coverage.missing
    assert REASON_FOREIGN_TEAM_IN_WINDOW in {
        error["reason"] for error in profile.errors
    }


# --------------------------------------------------------------------------
# The removal — tests that fail if the staff coupling comes back
# --------------------------------------------------------------------------

# Every field the deferred `coaching-staff` collector owns. A row carrying any
# of them is keyed to something this collector cannot know.
STAFF_FIELDS = (
    "revision_id",
    "effective_from_week",
    "effective_to_week",
    "head_coach_id",
    "head_coach_name",
    "offensive_coordinator_id",
    "defensive_coordinator_id",
    "play_caller_id",
    "play_caller_role",
    "play_caller_source",
    "play_caller_missing_reason",
    "change_event",
    "change_reported_at",
    "changepoint_unexplained",
    "changepoint_week",
    "changepoint_shift",
    "changepoint_p_value",
)


async def test_no_published_row_carries_a_staff_derived_field(lake: SpyLake):
    """**The removal, asserted on the product rather than on the source.**

    The schema's `additionalProperties: false` would reject an added field
    too, but only for fields nobody also added to the schema. This names them,
    so restoring `revision_id` in both places at once still fails here — and
    that is the shape the restoration would actually take, because the two are
    written to agree.
    """
    envelopes = await run_capture(lake=lake)
    rows = envelopes["team_scheme_profile"].signals
    assert rows
    for row in rows:
        present = sorted(set(row) & set(STAFF_FIELDS))
        assert not present, (
            f"{row['team_id']} publishes staff-derived field(s) {present}; "
            "those belong to the deferred coaching-staff collector"
        )


async def test_the_row_key_is_the_team_season_and_nothing_else(lake: SpyLake):
    """One row per `(team_id, season)`, and the pair is unique.

    A second key would show up here as a duplicate `(team_id, season)` — which
    is precisely what revision keying produced, and what the split removed.
    """
    envelopes = await run_capture(
        Feeds(proe=proe_with_shift("AAA", at_week=7, shift=20.0)), lake=lake
    )
    rows = envelopes["team_scheme_profile"].signals
    keys = [(row["team_id"], row["season"]) for row in rows]
    assert len(keys) == len(set(keys)), keys
    assert {season for _, season in keys} == {2026}


def test_the_collectors_source_names_no_staff_concept():
    """The other direction: the *code*, not the output.

    A restoration that reads a coach feed but has not yet reached the row
    shape would pass the two tests above while the coupling is already half
    back.

    **The match is exact identifier equality**, over `Name`, `Attribute`,
    `arg` and string-constant nodes — not a substring search. Two consequences,
    both deliberate and neither of them "docstrings are excluded":

    * Prose does not trip it. A docstring *containing* `revision_id` is one
      string constant that is not *equal* to it, which is why this file,
      `rates.py` and `capture.py` can all discuss the removal at length. A
      gate that forbade the words would forbid explaining them.
    * A determined rename passes. `revision_id_v2`, `rev_id`, or an f-string
      assembling the value under some other key would all get through. That is
      an accepted bound — no gate catches a rename — and it is why this test
      is the third layer rather than the only one: the row-shape assertion
      above and the schema's `additionalProperties: false` catch the value
      arriving on the wire whatever it is called.
    """
    import ast
    from pathlib import Path

    banned = {
        "revision_id",
        "effective_from_week",
        "effective_to_week",
        "head_coach_id",
        "play_caller_id",
        "changepoint",
    }
    package = Path(__file__).resolve().parents[1] / "team_scheme"
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            elif isinstance(node, ast.arg):
                name = node.arg
            if name in banned:
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert not offenders, (
        f"staff-derived identifiers are back in the collector's source: {offenders}"
    )

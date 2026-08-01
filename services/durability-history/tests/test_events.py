"""Event reconstruction — the actual task, tested against constructed histories.

The capture-level tests prove the pipeline. This file proves the reconstruction:
that consecutive weeks collapse into one event, that a bye does not split one,
that a return produces a `days_to_return` measured from a real upstream date, and
that a recurrence is decided by a rule rather than by a coincidence.

Built from `PlayerRow`/`GameRef`/`DesignationRow` directly rather than through
CSV, so a failure points at the reconstruction rather than at a fixture.
"""

from datetime import date, timedelta

import pytest

from durability_history.adapters.upstream import (
    DesignationRow,
    GameRef,
    Participation,
    PlayerRow,
    Schedule,
)
from durability_history.events import (
    BODY_PARTS,
    TISSUE_CLASSES,
    normalize_site,
    reconstruct,
    site_body_part,
    site_tissue_class,
)

SEASON = 2026
TEAM = "SEA"
PFR = "TestPl00"
GSIS = "00-0000042"
PLAYER_ID = "fdy-test00000001"


def player(*, pfr_id: str | None = PFR, rookie: int = 2024) -> PlayerRow:
    return PlayerRow(
        gsis_id=GSIS,
        pfr_id=pfr_id,
        display_name="Test Player",
        team=TEAM,
        position="RB",
        jersey_number=21,
        birth_date=date(2000, 1, 1),
        rookie_season=rookie,
    )


def schedule(
    weeks: int = 8, *, season: int = SEASON, bye: int | None = None
) -> Schedule:
    """A team's completed games. `bye` removes one week entirely, which is the
    whole reason runs are walked over the GAME list rather than week numbers."""
    games = tuple(
        GameRef(
            game_id=f"{season}_{week:02d}_BUF_{TEAM}",
            season=season,
            week=week,
            game_type="REG",
            gameday=date(season, 9, 6) + timedelta(days=7 * (week - 1)),
            team=TEAM,
        )
        for week in range(1, weeks + 1)
        if week != bye
    )
    return Schedule(by_team_season={(season, TEAM): games}, seasons_read=(season,))


def designations(*specs, season: int = SEASON) -> list[DesignationRow]:
    """`(week, primary_injury)` pairs as report lines, filed two days before the
    game so `onset_date` is a real upstream date rather than a derived one."""
    rows = []
    for week, primary in specs:
        gameday = date(season, 9, 6) + timedelta(days=7 * (week - 1))
        rows.append(
            DesignationRow(
                season=season,
                week=week,
                team=TEAM,
                gsis_id=GSIS,
                game_type="REG",
                report_status="Out",
                report_primary_injury=primary,
                report_secondary_injury="",
                practice_primary_injury=primary,
                practice_secondary_injury="",
                practice_status="Did Not Participate In Practice",
                reported_at=gameday - timedelta(days=2),
            )
        )
    return rows


def participation(*weeks: int, season: int = SEASON, pct: float = 0.8) -> Participation:
    result = Participation(seasons_read=(season,))
    for week in weeks:
        result.snap_pct[(PFR, season, week)] = pct
        result.team_of[(PFR, season, week)] = TEAM
    return result


def build(
    *,
    played,
    designated,
    weeks: int = 8,
    bye: int | None = None,
    seasons=None,
    window: int = 90,
):
    return reconstruct(
        player(),
        PLAYER_ID,
        seasons=seasons or [SEASON],
        schedule=schedule(weeks, bye=bye),
        designations=designated,
        participation=participation(*played),
        complete=True,
        recurrence_window_days=window,
    )


# ── collapsing runs ──────────────────────────────────────────────────────────


def test_consecutive_designated_weeks_collapse_into_ONE_event():
    """The core of "event reconstruction, not row copying". Three weekly report
    lines are one hamstring strain, not three."""
    history = build(
        played=[1, 2, 6, 7, 8],
        designated=designations((3, "Hamstring"), (4, "Hamstring"), (5, "Hamstring")),
    )
    assert len(history.events) == 1
    event = history.events[0]
    assert event.games_missed == 3
    assert event.onset_week == 3


def test_a_bye_week_does_not_split_one_absence_into_two_events():
    """Weeks 5 and 7 with a bye in week 6 are consecutive GAMES. Comparing week
    numbers instead produces two events and then flags the second as a recurrence
    of the first — a fabricated re-aggravation, at exactly the distance the
    recurrence window is calibrated for."""
    history = build(
        played=[1, 2, 3, 4, 8],
        designated=designations((5, "Hamstring"), (7, "Hamstring")),
        bye=6,
    )
    assert len(history.events) == 1, [e.event_id for e in history.events]
    assert history.events[0].is_recurrence_of is None


def test_two_different_body_parts_in_consecutive_weeks_are_two_events():
    """A run is a run of the SAME site. A knee that follows a hamstring is a new
    injury, not week four of the old one."""
    history = build(
        played=[1, 5, 6, 7, 8],
        designated=designations((2, "Hamstring"), (3, "Hamstring"), (4, "Knee")),
    )
    assert [e.injury_site for e in history.events] == ["hamstring", "knee"]


def test_a_non_injury_designation_ENDS_a_run_rather_than_extending_it():
    """A suspension in the middle of a hamstring absence must not merge two
    events into one long one — nor contribute a missed game to either."""
    history = build(
        played=[1, 7, 8],
        designated=[
            *designations((2, "Hamstring")),
            *designations((3, "Not injury related - coach's decision")),
            *designations((4, "Hamstring")),
        ],
    )
    assert len(history.events) == 2
    assert sum(e.games_missed for e in history.events) == 2
    assert history.games_missed_injury == 2
    reasons = [a.absence_reason for a in history.absences]
    assert reasons.count("discipline") == 1


def _two_season_schedule(weeks: int = 6) -> Schedule:
    """Two consecutive seasons for one club, so a run can be offered a boundary
    to cross."""
    by_team_season = {}
    for season in (SEASON - 1, SEASON):
        by_team_season[(season, TEAM)] = tuple(
            GameRef(
                game_id=f"{season}_{week:02d}_BUF_{TEAM}",
                season=season,
                week=week,
                game_type="REG",
                gameday=date(season, 9, 6) + timedelta(days=7 * (week - 1)),
                team=TEAM,
            )
            for week in range(1, weeks + 1)
        )
    return Schedule(by_team_season=by_team_season, seasons_read=(SEASON - 1, SEASON))


def _across_seasons(*, played, designated):
    return reconstruct(
        player(),
        PLAYER_ID,
        seasons=[SEASON - 1, SEASON],
        schedule=_two_season_schedule(),
        designations=designated,
        participation=played,
        complete=True,
    )


def test_a_run_does_NOT_cross_a_season_boundary():
    """An offseason is not a bye.

    A hamstring designated in the last week of one season and again in Week 1 of
    the next is two events. Collapsed into one, it publishes a `days_to_return`
    of ~339 — mostly offseason — which lands in
    `median_days_to_return_by_body_part` and in `/signals/return-profile`, the
    route whose entire purpose is telling a generator when a player comes back.
    It also hides a decision the recurrence rule should have made: split
    correctly, the second event is well outside the 90-day window and is
    correctly novel.
    """
    played = participation(1, 2, 3, 4, 5, season=SEASON - 1)
    for week in (2, 3, 4, 5, 6):
        played.snap_pct[(PFR, SEASON, week)] = 0.8
        played.team_of[(PFR, SEASON, week)] = TEAM

    history = _across_seasons(
        played=played,
        designated=[
            *designations((6, "Hamstring"), season=SEASON - 1),
            *designations((1, "Hamstring"), season=SEASON),
        ],
    )

    assert len(history.events) == 2, [e.event_id for e in history.events]
    assert [e.onset_season for e in history.events] == [SEASON - 1, SEASON]
    assert all(
        e.days_to_return is None or e.days_to_return < 60 for e in history.events
    ), [e.days_to_return for e in history.events]
    # The decision the collapse would have hidden.
    assert history.events[1].is_recurrence_of is None


def test_an_event_unresolved_when_the_season_ENDS_stays_unresolved():
    """The resolution scan is bounded the same way the run is. A return measured
    across an offseason is not a recovery time, and `None` is the honest answer:
    the games stopped, so we do not know."""
    played = participation(1, 2, 3, 4, 5, season=SEASON - 1)
    for week in range(1, 7):
        played.snap_pct[(PFR, SEASON, week)] = 0.8
        played.team_of[(PFR, SEASON, week)] = TEAM

    history = _across_seasons(
        played=played, designated=designations((6, "Knee"), season=SEASON - 1)
    )

    knee = next(e for e in history.events if e.injury_site == "knee")
    assert knee.days_to_return is None
    assert knee.resolved_date is None


# ── resolution ───────────────────────────────────────────────────────────────


def test_days_to_return_is_measured_from_the_report_date_to_the_return_game():
    """`onset_date` is "the first date the injury was recorded" — the earliest
    `date_modified` on the run, which is a real upstream value. The return is the
    first game the player actually played after it."""
    history = build(played=[1, 4, 5], designated=designations((2, "Knee"), (3, "Knee")))
    event = history.events[0]
    # Reported Friday of week 2 (4 Sep + 7 - 2 days), returned in week 4.
    assert event.onset_date == date(SEASON, 9, 11)
    assert event.resolved_date == date(SEASON, 9, 27)
    assert event.days_to_return == 16


def test_an_event_the_player_never_returns_from_is_UNRESOLVED_not_zero():
    """Null, never 0. A player still out is the single most relevant fact when
    the question is "when does he come back", and a 0 would read as "returned the
    same day"."""
    history = build(played=[1, 2], designated=designations((3, "Ankle"), (4, "Ankle")))
    event = history.events[0]
    assert event.days_to_return is None
    assert event.resolved_date is None
    assert event.resolved is False


def test_an_unresolved_event_does_not_count_toward_the_sample_size():
    """`sample_size_events` is "resolved events backing the aggregates", and an
    event with no return time backs nothing."""
    history = build(played=[1, 2], designated=designations((3, "Ankle")))
    assert len(history.events) == 1
    from durability_history import derive

    assert derive.sample_size_events(history.events) == 0


def test_an_event_the_player_plays_through_is_still_an_event():
    """A Questionable hamstring the player suits up for is a strained hamstring.
    Dropping it would hide the two strains that preceded the one that finally
    cost a game — which is the recurrence history the whole collector is for."""
    history = build(played=list(range(1, 9)), designated=designations((3, "Hamstring")))
    assert len(history.events) == 1
    assert history.events[0].games_missed == 0
    assert history.games_missed_injury == 0


def test_a_played_through_event_gets_NO_return_time():
    """He never left, so there is nothing to return from.

    Handing the event the next game's date manufactures a `days_to_return` out of
    the ordinary weekly cadence — the event is published (that is right), but it
    must not be able to back a return statistic.
    """
    history = build(played=list(range(1, 9)), designated=designations((3, "Hamstring")))
    event = history.events[0]
    assert event.games_missed == 0
    assert event.days_to_return is None
    assert event.resolved_date is None
    assert event.resolved is False


def test_a_never_unavailable_player_cannot_unlock_return_statistics():
    """The whole point of the previous test, at the aggregate level.

    Three played-through Questionable hamstrings and ZERO missed games would
    otherwise publish three 9-day "returns", cross `MIN_SAMPLE_EVENTS`, and
    unlock every derived rate — including a `soft_tissue_recurrence_rate` of
    0.667 — for a player who has never been unavailable, while
    `career_games_missed_injury` correctly read 0. Two published numbers about
    the same player, disagreeing.
    """
    from durability_history import derive

    history = build(
        played=list(range(1, 9)),
        designated=designations((3, "Hamstring"), (5, "Hamstring"), (7, "Hamstring")),
    )

    assert len(history.events) == 3
    assert history.games_missed_injury == 0
    assert derive.sample_size_events(history.events) == 0
    assert derive.median_days_to_return_by_body_part(history.events) is None
    assert derive.soft_tissue_recurrence_rate(history.events) is None
    assert derive.post_return_snap_trajectory(history) is None
    # The events themselves survive — the evidence is still published.
    assert derive.body_part_history(history.events)["hamstring"]["event_count"] == 3


def test_an_event_the_player_DID_miss_games_for_still_resolves():
    """The guard must not be so wide that it stops resolving real absences —
    every return time in the fleet would go null and the route would publish
    nothing."""
    history = build(played=[1, 4, 5], designated=designations((2, "Knee"), (3, "Knee")))
    assert history.events[0].games_missed == 2
    assert history.events[0].days_to_return == 16
    assert history.events[0].resolved is True


# ── recurrence ───────────────────────────────────────────────────────────────


def test_a_same_site_event_inside_the_window_is_a_RECURRENCE():
    """The spec's own example: a hamstring 26 days after the last one."""
    history = build(
        played=[1, 3, 4, 6, 7, 8],
        designated=designations((2, "Hamstring"), (5, "Hamstring")),
    )
    assert len(history.events) == 2
    assert history.events[1].is_recurrence_of == history.events[0].event_id


def test_a_same_site_event_OUTSIDE_the_window_is_a_NOVEL_injury():
    """The rule is a rule, not a vibe: outside the configured window the same
    body part is a new injury, and a collector that linked them regardless would
    call every player's second hamstring a re-aggravation forever."""
    history = build(
        played=[1, 3, 4, 6, 7, 8],
        designated=designations((2, "Hamstring"), (5, "Hamstring")),
        window=1,
    )
    assert history.events[1].is_recurrence_of is None


def test_a_recurrence_links_to_the_NEAREST_prior_event_not_the_first():
    """Three hamstrings should chain, each to the one before it. Linking all
    three to the first would make a two-year-old strain the parent of a strain
    that happened last month."""
    history = build(
        played=[1, 3, 5, 7, 8],
        designated=designations((2, "Hamstring"), (4, "Hamstring"), (6, "Hamstring")),
    )
    assert len(history.events) == 3
    assert history.events[0].is_recurrence_of is None
    assert history.events[1].is_recurrence_of == history.events[0].event_id
    assert history.events[2].is_recurrence_of == history.events[1].event_id


def test_a_different_site_is_never_a_recurrence():
    history = build(
        played=[1, 3, 4, 6, 7, 8],
        designated=designations((2, "Hamstring"), (5, "Knee")),
    )
    assert history.events[1].is_recurrence_of is None


def test_recurrence_keys_on_injury_site_not_the_coarse_body_part():
    """A calf and a quad are both `body_part: other` under the spec's ten-value
    enum. Keying recurrence on `body_part` would link them as one re-aggravated
    tissue — a fabricated recurrence, which is the same class of error as a
    fabricated absence.

    Both events are published with `body_part: other`, so this is provable ONLY
    through `injury_site`, which is exactly why it is a published field.
    """
    history = build(
        played=[1, 3, 4, 6, 7, 8],
        designated=designations((2, "Calf"), (5, "Quadricep")),
    )
    assert [e.body_part for e in history.events] == ["other", "other"]
    assert [e.injury_site for e in history.events] == ["calf", "quadricep"]
    assert history.events[1].is_recurrence_of is None


def test_the_event_id_is_stable_across_two_identical_reconstructions():
    """`is_recurrence_of` points at an `event_id`, and a key that changed between
    passes would make every historical link dangle."""
    kwargs = dict(
        played=[1, 3, 4, 5, 6, 7, 8], designated=designations((2, "Hamstring"))
    )
    assert [e.event_id for e in build(**kwargs).events] == [
        e.event_id for e in build(**kwargs).events
    ]
    assert build(**kwargs).events[0].event_id == f"{PLAYER_ID}:2026-W02:hamstring"


# ── tenure ───────────────────────────────────────────────────────────────────


def test_tenure_is_bounded_by_what_was_observed_not_by_the_whole_season():
    """A player signed in week 5 was not around for weeks 1-4.

    Counting those inflates `career_games_possible`, which LOWERS
    `availability_rate` — manufacturing exactly the durability problem the named
    failure mode is about. The bias direction is why this is a hard bound.
    """
    history = build(played=[5, 6, 7, 8], designated=[])
    assert history.games_possible == 4
    assert history.games_missed_injury == 0
    assert [entry.game.week for entry in history.tenure] == [5, 6, 7, 8]


def test_a_gap_inside_the_observed_span_IS_a_tenure_game():
    """The other side of the bound. A player who played weeks 1 and 8 was around
    for weeks 2-7, and the games he missed in between are games he could have
    played — undesignated, and therefore not injuries, but counted as possible.
    """
    history = build(played=[1, 8], designated=[])
    assert history.games_possible == 8
    assert len(history.absences) == 6
    assert {a.absence_reason for a in history.absences} == {"undesignated"}
    assert history.games_missed_injury == 0


def test_a_player_with_no_pfr_id_still_gets_a_designation_only_tenure():
    """`snap_counts` carries no GSIS id, so a player with no `pfr_id` has no
    participation row anywhere. His tenure comes from the injury report alone,
    which is a truncated answer — not a reason to publish nothing."""
    history = reconstruct(
        player(pfr_id=None),
        PLAYER_ID,
        seasons=[SEASON],
        schedule=schedule(),
        designations=designations((3, "Ankle")),
        participation=Participation(seasons_read=(SEASON,)),
        complete=True,
    )
    assert history.games_possible == 1
    assert history.games_missed_injury == 1
    assert len(history.events) == 1


def test_a_season_nothing_observed_contributes_no_games():
    """Neither a snap nor a report line means this collector has no evidence the
    player was on a roster at all, and inventing seventeen games he could have
    played would invent seventeen absences to explain."""
    history = build(played=[], designated=[], seasons=[2025, SEASON])
    assert history.games_possible == 0
    assert history.absences == ()


def test_designated_games_counts_every_report_line_in_tenure():
    """The right-hand side of the spec's assertion. Every line, injury or not —
    it is "games where the injury report carried a designation", which is what
    makes the assertion meaningful for a player whose absences were all
    suspensions."""
    history = build(
        played=[1, 2, 5, 6, 7, 8],
        designated=[
            *designations((3, "Hamstring")),
            *designations((4, "Not injury related - personal matter")),
        ],
    )
    assert history.designated_games == 2
    assert history.games_missed_injury == 1


# ── the site vocabulary ──────────────────────────────────────────────────────


def test_every_site_maps_into_the_specs_closed_enums():
    """A body part or tissue class outside the spec's enum fails contract
    conformance for every row that player appears in, which is a schema failure
    six weeks after the vocabulary edit that caused it."""
    from durability_history.events import _SITES

    for site in _SITES:
        assert site_body_part(site) in BODY_PARTS, site
        assert site_tissue_class(site) in TISSUE_CLASSES, site


@pytest.mark.parametrize(
    ("text", "site"),
    [
        ("Left Hamstring", "hamstring"),
        ("Right Hamstring", "hamstring"),
        ("Hamstring, Abdomen", "hamstring"),
        ("Quad", "quadricep"),
        ("Right Quadricep", "quadricep"),
        ("Ribs", "rib"),
        ("Feet", "foot"),
        ("Third injury: Ankle", "ankle"),
        ("gameday concussion protocol evaluation", "concussion"),
        ("", "unspecified"),
        ("--", "unspecified"),
    ],
)
def test_site_normalization_folds_the_real_upstream_spellings(text, site):
    """Every one of these is a literal string in `injuries_2024.csv`. Laterality
    is discarded because the same club writes `Hamstring` one week and `Left
    Hamstring` the next for the same injury, and a lateral key would split one
    event in two and then call the second half a recurrence."""
    assert normalize_site(text) == site


def test_an_unrecognised_site_is_unspecified_rather_than_its_own_key():
    """A site this collector cannot place must not become its own recurrence key,
    or every idiosyncratic club spelling would look like a distinct body part
    that never recurs."""
    assert normalize_site("Left Metatarsal Widget") == "unspecified"
    assert site_body_part("unspecified") == "other"

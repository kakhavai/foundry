"""The revision timeline, and the coach-id scheme underneath it.

`effective_to_week` is derived from the NEXT record and never stored on both
sides. That is `venue`'s shape and the reason two revisions cannot disagree
about where the boundary is — an edit to one side of a stored pair leaves a
gap or an overlap that nothing detects.
"""

import pytest

from coaching_scheme.adapters.games import TeamWeekCoach
from coaching_scheme.revisions import (
    CHANGE_NONE,
    CHANGE_UNCLASSIFIED,
    build_revisions,
    coach_id,
)

SEASON = 2026


def rows(team: str, weeks: dict[int, str]) -> list[TeamWeekCoach]:
    return [TeamWeekCoach(team, week, name) for week, name in sorted(weeks.items())]


# --------------------------------------------------------------------------
# coach_id
# --------------------------------------------------------------------------


def test_a_name_becomes_a_stable_slug():
    assert coach_id("Nick Sirianni") == "coach-nick-sirianni"


def test_the_slug_is_deterministic_across_calls():
    """It is the join key between two passes and between two seasons. A
    non-deterministic id would make every pass look like a coaching change."""
    assert coach_id("Sean McDermott") == coach_id("Sean McDermott")


def test_accents_are_folded_rather_than_dropped():
    """Two spellings of one name must not diverge on a byte. Dropping the
    character instead of folding it would make `Munoz` and `Muñoz` different
    people."""
    assert coach_id("Muñoz") == coach_id("Munoz") == "coach-munoz"


def test_punctuation_and_case_normalise():
    assert coach_id("  D.J.  O'Neill-Smith ") == "coach-d-j-o-neill-smith"


def test_an_empty_name_has_no_id():
    """A blank coach cell is a missing name, not a coach called ''."""
    assert coach_id("") is None
    assert coach_id("   ") is None
    assert coach_id("!!!") is None


# --------------------------------------------------------------------------
# build_revisions
# --------------------------------------------------------------------------


def test_a_steady_season_is_one_revision():
    revisions = build_revisions(
        rows("AAA", {week: "Steady Coach" for week in range(1, 18)}), season=SEASON
    )["AAA"]
    assert len(revisions) == 1
    assert revisions[0].effective_from_week == 1
    assert revisions[0].effective_to_week is None
    assert revisions[0].change_event == CHANGE_NONE
    assert revisions[0].revision_id == f"AAA-{SEASON}-r1"


def test_a_mid_season_change_splits_into_two_revisions():
    weeks = {week: "First" for week in range(1, 7)}
    weeks.update({week: "Second" for week in range(7, 13)})
    revisions = build_revisions(rows("AAA", weeks), season=SEASON)["AAA"]

    assert len(revisions) == 2
    assert (revisions[0].effective_from_week, revisions[0].effective_to_week) == (1, 6)
    assert (revisions[1].effective_from_week, revisions[1].effective_to_week) == (
        7,
        None,
    )
    assert revisions[0].change_event == CHANGE_NONE
    assert revisions[1].change_event == CHANGE_UNCLASSIFIED


def test_the_end_is_derived_from_the_next_records_start():
    """Not stored independently. A boundary at week 7 means the previous
    revision ends at 6, with no gap and no overlap — by construction rather
    than by two numbers happening to agree."""
    weeks = {week: "First" for week in range(1, 5)}
    weeks.update({week: "Second" for week in range(9, 13)})
    revisions = build_revisions(rows("AAA", weeks), season=SEASON)["AAA"]
    assert revisions[0].effective_to_week == revisions[1].effective_from_week - 1


def test_a_bye_week_does_not_split_a_revision():
    """A team is absent from the grid on its bye. Treating that as a boundary
    would manufacture a regime change every team has once a season."""
    weeks = {week: "Steady" for week in range(1, 13) if week != 7}
    revisions = build_revisions(rows("AAA", weeks), season=SEASON)["AAA"]
    assert len(revisions) == 1
    assert revisions[0].effective_from_week == 1


def test_a_coach_who_returns_gets_a_third_revision():
    """Runs are maximal and consecutive, not a set of distinct names. A
    dedupe by name would collapse weeks 1-3 and 9-12 into one revision and
    lose the fact that somebody else ran the team in between."""
    weeks = {1: "A", 2: "A", 3: "A", 4: "B", 5: "B", 6: "B", 7: "A", 8: "A", 9: "A"}
    revisions = build_revisions(rows("AAA", weeks), season=SEASON)["AAA"]
    assert [r.effective_from_week for r in revisions] == [1, 4, 7]
    assert revisions[0].head_coach_id == revisions[2].head_coach_id


def test_a_blank_coach_still_produces_a_revision():
    """The team plays and owes a row; what is missing is the name."""
    revisions = build_revisions(
        rows("AAA", {week: "" for week in range(1, 5)}), season=SEASON
    )["AAA"]
    assert len(revisions) == 1
    assert revisions[0].head_coach_id is None
    assert revisions[0].head_coach_name is None


def test_the_head_coach_name_travels_with_the_id():
    """The id is a slug OF the name, so a re-spelling upstream mints a new id
    that reads as a coaching change. Publishing the name beside it is what
    lets a consumer tell those apart."""
    revisions = build_revisions(rows("AAA", {1: "Nick Sirianni"}), season=SEASON)["AAA"]
    assert revisions[0].head_coach_id == "coach-nick-sirianni"
    assert revisions[0].head_coach_name == "Nick Sirianni"


# --------------------------------------------------------------------------
# covers / week_span — the arithmetic guard 1 rests on
# --------------------------------------------------------------------------


def test_covers_is_inclusive_at_both_ends():
    weeks = {week: "First" for week in range(1, 7)}
    weeks.update({week: "Second" for week in range(7, 13)})
    first, second = build_revisions(rows("AAA", weeks), season=SEASON)["AAA"]

    assert first.covers(1) and first.covers(6)
    assert not first.covers(7)
    assert second.covers(7) and second.covers(99)
    assert not second.covers(6)


def test_an_open_revision_does_not_cover_a_week_before_it_starts():
    """`effective_to_week is None` means *current*, not *always*. A `covers`
    that only checked the upper bound would say every open revision covers
    week 1 — and every team's last revision is open."""
    revisions = build_revisions(
        rows("AAA", {week: "Late" for week in range(9, 13)}), season=SEASON
    )["AAA"]
    assert not revisions[0].covers(3)
    assert revisions[0].covers(9)


@pytest.mark.parametrize(
    ("weeks", "expected_spans"),
    [
        ({week: "One" for week in range(1, 13)}, [12]),
        (
            {
                **{week: "One" for week in range(1, 7)},
                **{week: "Two" for week in range(7, 13)},
            },
            [6, 6],
        ),
    ],
)
def test_week_span_counts_inclusively(weeks, expected_spans):
    """The ceiling guard 1's second arm compares `games_sampled` against. Off
    by one here silently permits one extra game per revision, which is exactly
    the duplicate fold that arm exists to catch."""
    revisions = build_revisions(rows("AAA", weeks), season=SEASON)["AAA"]
    assert [r.week_span for r in revisions] == expected_spans


def test_an_open_revisions_span_stops_at_the_last_known_week():
    """Otherwise arm B has no ceiling for the one revision every team has."""
    weeks = {week: "One" for week in range(1, 7)}
    weeks.update({week: "Two" for week in range(7, 10)})
    revisions = build_revisions(rows("AAA", weeks), season=SEASON)["AAA"]
    assert revisions[1].effective_to_week is None
    assert revisions[1].week_span == 3

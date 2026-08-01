"""Crew identity and crew churn.

The spec's adapter note is what this file tests: *"an adapter must resolve
individual officials, not just the referee's name, because crews are
reassembled between seasons and substituted within them — attributing rates to
a name is only valid if the group behind that name is stable."*
"""

import pytest

from officiating.adapters.officials import Official
from officiating.crews import (
    CONTINUITY_ALARM_THRESHOLD,
    CrewAssignment,
    build_assignments,
    continuity_pct,
    crew_id_for,
    referee_disagreement,
    referee_of,
    undersized,
)

POSITIONS = (
    "Umpire",
    "Down Judge",
    "Line Judge",
    "Field Judge",
    "Side Judge",
    "Back Judge",
)


def _crew(referee_id: str, member_ids: tuple[str, ...]) -> tuple[Official, ...]:
    return (
        Official(referee_id, f"Ref {referee_id}", "Referee"),
        *(
            Official(member_id, f"Official {member_id}", POSITIONS[index])
            for index, member_id in enumerate(member_ids)
        ),
    )


def _assignment(
    game_id: str, week: int, referee_id: str, member_ids: tuple[str, ...]
) -> CrewAssignment:
    return CrewAssignment(
        game_id=game_id,
        legacy_game_id=f"legacy-{game_id}",
        week=week,
        crew_id=crew_id_for(2026, referee_id),
        referee_id=referee_id,
        referee_name=f"Ref {referee_id}",
        members=_crew(referee_id, member_ids),
    )


REGULARS = ("101", "102", "103", "104", "105", "106")


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_crew_id_is_keyed_by_official_id_and_scoped_to_the_season():
    """Both halves matter. Keyed by name, a nickname split one real crew into
    two on 17 of 272 games of the real 2025 season; unscoped by season, two
    genuinely different crews under the same white hat would pool."""
    assert crew_id_for(2026, "693") == "2026-ref693"
    assert crew_id_for(2025, "693") != crew_id_for(2026, "693")


def test_referee_of_returns_none_rather_than_guessing():
    """Without a white hat there is no crew key. Falling back to the umpire
    would invent a crew that exists in no other week and whose rates are drawn
    from a single game."""
    assert referee_of(_crew("693", REGULARS)).official_id == "693"
    assert referee_of(_crew("693", REGULARS)[1:]) is None
    assert referee_of([]) is None


def test_build_assignments_reports_unpublished_games_as_misses():
    """The post-game-feed case, at the unit level: a scheduled game the
    officials feed has not reached is a MISS with a reason, never a game that
    quietly left the universe."""
    assignments, misses = build_assignments(
        season=2026,
        games_by_legacy_id={"L1": ("2026_01_A_B", 1), "L2": ("2026_01_C_D", 1)},
        officials_by_legacy_id={"L1": list(_crew("693", REGULARS))},
    )

    assert len(assignments) == 1
    assert assignments[0].game_id == "2026_01_A_B"
    assert misses == {"2026_01_C_D": "crew_not_published"}


def test_build_assignments_reports_a_refereeless_crew_separately():
    """A different fact from "not published yet", and a different operator
    action: one is the season progressing, the other is the feed changing
    shape."""
    _assignments, misses = build_assignments(
        season=2026,
        games_by_legacy_id={"L1": ("2026_01_A_B", 1)},
        officials_by_legacy_id={"L1": list(_crew("693", REGULARS)[1:])},
    )

    assert misses == {"2026_01_A_B": "no_referee_in_crew"}


def test_members_are_ordered_deterministically():
    """The rows feed a content digest, so an unordered member list would make
    every pass's digest unique and silently disable the unchanged-snapshot
    gate."""
    forward = build_assignments(
        season=2026,
        games_by_legacy_id={"L1": ("g1", 1)},
        officials_by_legacy_id={"L1": list(_crew("693", REGULARS))},
    )[0][0]
    reversed_input = build_assignments(
        season=2026,
        games_by_legacy_id={"L1": ("g1", 1)},
        officials_by_legacy_id={"L1": list(reversed(_crew("693", REGULARS)))},
    )[0][0]

    assert forward.members == reversed_input.members


def test_undersized_is_reported_without_dropping_the_assignment():
    """Who worked the game is a fact even when the feed is incomplete about
    it. The row is published; the shortfall is visible on a metric."""
    full = _assignment("g1", 1, "693", REGULARS)
    short = _assignment("g2", 1, "693", REGULARS[:3])

    assert undersized(full) is False
    assert undersized(short) is True


# ---------------------------------------------------------------------------
# continuity
# ---------------------------------------------------------------------------


def test_a_stable_crew_has_full_continuity():
    window = [_assignment(f"g{w}", w, "693", REGULARS) for w in range(1, 9)]

    assert continuity_pct(window[0], window) == pytest.approx(1.0)


def test_one_substitute_lowers_continuity_proportionally():
    """A single one-off substitute in an eight-game window contributes 1/8 of a
    member's worth, so a seven-person crew lands at (6 + 0.125)/7."""
    window = [_assignment(f"g{w}", w, "693", REGULARS) for w in range(1, 8)]
    substituted = _assignment("g8", 8, "693", ("999", *REGULARS[1:]))
    window.append(substituted)

    assert continuity_pct(substituted, window) == pytest.approx((6 + 0.125) / 7)
    # And it does NOT drag down the crew's other assignments, which is the
    # point of computing this per assignment rather than per crew.
    assert continuity_pct(window[0], window) == pytest.approx((6 + 7 / 8) / 7)


def test_a_wholly_reassembled_crew_falls_below_the_alarm_threshold():
    """The condition the spec names: rates describing people who are not
    working the game."""
    window = [_assignment(f"g{w}", w, "693", REGULARS) for w in range(1, 9)]
    reassembled = _assignment(
        "g9", 9, "693", ("901", "902", "903", "904", "905", "906")
    )

    value = continuity_pct(reassembled, [*window, reassembled])

    assert value < CONTINUITY_ALARM_THRESHOLD
    assert value == pytest.approx((1.0 + 6 * (1 / 9)) / 7)


def test_an_empty_window_is_one_not_zero():
    """Nothing is known about churn, and 0.0 would read as TOTAL churn — it
    would trip the alarm on every assignment of a crew whose rates are not
    being served anyway, which is the one combination that must stay quiet."""
    assignment = _assignment("g1", 1, "693", REGULARS)

    assert continuity_pct(assignment, []) == 1.0


def test_continuity_is_bounded_to_the_unit_interval():
    """The schema declares `minimum: 0, maximum: 1`, so a formula that could
    exceed 1 would fail conformance rather than merely being wrong."""
    window = [_assignment(f"g{w}", w, "693", REGULARS) for w in range(1, 5)]

    for assignment in window:
        assert 0.0 <= continuity_pct(assignment, window) <= 1.0


# ---------------------------------------------------------------------------
# the referee cross-check
# ---------------------------------------------------------------------------


def test_a_nickname_is_not_a_disagreement():
    """The real finding: `games.csv` says "Ron Torbert" and `officials.csv`
    says "Ronald Torbert" on all 17 of that crew's 2025 games. Compared
    strictly that is a phantom eighteenth crew; compared on surname it is a
    footnote."""
    assignment = CrewAssignment(
        game_id="g1",
        legacy_game_id="L1",
        week=1,
        crew_id="2025-ref495",
        referee_id="495",
        referee_name="Ronald Torbert",
        members=_crew("495", REGULARS),
    )

    assert referee_disagreement(assignment, "Ron Torbert") is False
    assert referee_disagreement(assignment, "R. Torbert") is False


def test_a_different_surname_is_a_disagreement():
    """What the loose comparison must still catch: a crosswalk that has started
    joining the wrong games together."""
    assignment = CrewAssignment(
        game_id="g1",
        legacy_game_id="L1",
        week=1,
        crew_id="2025-ref495",
        referee_id="495",
        referee_name="Ronald Torbert",
        members=_crew("495", REGULARS),
    )

    assert referee_disagreement(assignment, "Carl Cheffers") is True


def test_a_missing_name_on_either_side_is_not_a_disagreement():
    """An absent value is not evidence of conflict, and counting it as one
    would make the metric fire on a feed that simply stopped populating the
    column."""
    assignment = CrewAssignment(
        game_id="g1",
        legacy_game_id="L1",
        week=1,
        crew_id="2025-ref495",
        referee_id="495",
        referee_name="Ronald Torbert",
        members=_crew("495", REGULARS),
    )

    assert referee_disagreement(assignment, "") is False

"""The config is the denominator, so it gets tested like one."""

from roster_scope.rules import (
    ALL_RULES,
    TEAMS,
    canonical_position,
    canonical_team,
    expected_slots,
    rule_by_id,
    slot_key,
)


def test_thirty_two_teams():
    assert len(TEAMS) == 32
    assert len(set(TEAMS)) == 32


def test_expected_slots_is_four_hundred_and_sixteen():
    """32 x (QB 2 + RB 3 + WR 4 + TE 2 + K 1 + DST 1) = 32 x 13 = 416.

    Pinned as a literal, not recomputed from the same rules the production
    code sums — a test that recomputes the formula passes no matter what the
    formula becomes.
    """
    assert len(expected_slots()) == 416


def test_expected_slots_are_unique():
    """A duplicated key would silently shrink `expected` inside the
    accumulator's set, which is exactly the deflated-denominator failure this
    collector exists to avoid."""
    slots = expected_slots()
    assert len(set(slots)) == len(slots)


def test_every_team_gets_every_rule():
    slots = set(expected_slots())
    for team in TEAMS:
        for rule in ALL_RULES:
            for rank in range(1, rule.max_depth + 1):
                assert slot_key(team, rule.rule_id, rank) in slots


def test_expected_slots_does_not_depend_on_any_upstream():
    """Called twice with nothing in between; identical both times. The whole
    point is that the denominator is config-derived and fixed before a chart
    is ever fetched."""
    assert expected_slots() == expected_slots()


def test_position_aliases_collapse_to_canonical_groups():
    for label in ("WR", "SE", "FL", "SLOT", "X", "Z"):
        assert canonical_position(label) == "WR"
    for label in ("RB", "HB", "TB"):
        assert canonical_position(label) == "RB"
    assert canonical_position("TE") == "TE"
    assert canonical_position("PK") == "K"
    assert canonical_position("wr") == "WR"


def test_unknown_position_is_none_not_passed_through():
    """A linebacker must not be able to occupy a WR slot."""
    assert canonical_position("LB") is None
    assert canonical_position("") is None


def test_team_aliases_and_unknown_teams():
    assert canonical_team("JAC") == "JAX"
    assert canonical_team("LA") == "LAR"
    assert canonical_team("kc") == "KC"
    assert canonical_team("XYZ") is None
    assert canonical_team("") is None


def test_rule_lookup():
    assert rule_by_id("wr_depth_le_4").max_depth == 4
    assert rule_by_id("all_team_defenses").entity_type == "team_defense"
    assert rule_by_id("nonexistent") is None

"""The config is the denominator, so it gets tested like one."""

import pytest

from roster_scope.rules import (
    ALL_RULES,
    MATCHUP_RULES,
    TEAMS,
    canonical_position,
    canonical_team,
    expected_matchup_slots,
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
    """A player-scope rule (QB/RB/WR/TE/K/DST) must never match a label
    outside its own group.

    `LB` used to prove this by being unmapped entirely; the matchup scope
    (Task 4) now maps it to the `LB` canonical group instead. That still
    keeps a linebacker off a `WR<=4` slot -- `LB` is not a player-scope
    rule's `position` -- so the property this test protects is unchanged.
    `test_an_unrecognised_label_is_dropped_not_guessed` below covers the
    still-unmapped case that `LB` used to stand in for.
    """
    assert canonical_position("NOT_A_POSITION") is None
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


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CB", "CB"),
        ("DB", "CB"),
        ("NB", "CB"),
        ("cb", "CB"),
        ("S", "S"),
        ("FS", "S"),
        ("SS", "S"),
        ("SAF", "S"),
        ("LB", "LB"),
        ("ILB", "LB"),
        ("OLB", "LB"),
        ("MLB", "LB"),
        ("EDGE", "LB"),
        ("DL", "DL"),
        ("DE", "DL"),
        ("DT", "DL"),
        ("NT", "DL"),
        ("OL", "OL"),
        ("LT", "OL"),
        ("LG", "OL"),
        ("C", "OL"),
        ("RG", "OL"),
        ("RT", "OL"),
        ("QB", "QB"),
        ("WR", "WR"),  # offensive mapping is unchanged
    ],
)
def test_defensive_and_line_labels_collapse_to_canonical_groups(raw, expected):
    assert canonical_position(raw) == expected


@pytest.mark.parametrize("raw", ["P", "LS", "KR", "ATH", "", "   ", "NOT_A_POSITION"])
def test_an_unrecognised_label_is_dropped_not_guessed(raw):
    """Returning None keeps a punter off a CB slot. The slot then reads as
    missing, which is the honest outcome."""
    assert canonical_position(raw) is None


def test_matchup_rules_are_role_matched_with_the_agreed_quotas():
    quotas = {rule.position: rule.max_depth for rule in MATCHUP_RULES}
    assert quotas == {"CB": 4, "S": 3, "LB": 3, "DL": 4, "OL": 5}, quotas
    assert len(MATCHUP_RULES) == 5


def test_expected_matchup_slots_is_config_derived_not_fetch_derived():
    """Computed from config alone, BEFORE any upstream is contacted -- which
    is what stops a truncated depth chart shrinking the denominator and
    reporting ratio 1.0 on a hole."""
    assert expected_matchup_slots() == len(TEAMS) * (4 + 3 + 3 + 4 + 5)
    assert expected_matchup_slots() == 608


def test_matchup_rule_ids_are_unique_and_distinct_from_the_player_scope():
    matchup_ids = [rule.rule_id for rule in MATCHUP_RULES]
    assert len(matchup_ids) == len(set(matchup_ids))
    assert not set(matchup_ids) & {rule.rule_id for rule in ALL_RULES}
    assert len(matchup_ids) == 5

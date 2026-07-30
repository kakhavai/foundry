"""The canonical record's own rules: the match key, the suffix split, id
minting, and position grouping."""

from datetime import UTC, datetime

import pytest

from player_identity.identity import (
    CROSSWALK_KEYS,
    crosswalk_external_ids,
    mint_player_id,
    normalized_key,
    position_group,
    roster_status,
    split_suffix,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Davante Adams", "davante adams"),
        ("DAVANTE ADAMS", "davante adams"),
        ("Ja'Marr Chase", "jamarr chase"),
        # The hyphen becomes a space rather than vanishing, so a book writing
        # "Amon Ra St Brown" lands on the same key as the roster feed.
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("Amon Ra St Brown", "amon ra st brown"),
        # Diacritics fold rather than surviving into the key.
        ("Tomás Núñez", "tomas nunez"),
        # The suffix never pollutes the key.
        ("Odell Beckham Jr.", "odell beckham"),
        ("Odell Beckham", "odell beckham"),
        ("Robert Griffin III", "robert griffin"),
        ("  spaced   out  ", "spaced out"),
        ("", ""),
    ],
)
def test_normalized_key(raw, expected):
    assert normalized_key(raw) == expected


@pytest.mark.parametrize(
    ("raw", "base", "suffix"),
    [
        ("Odell Beckham Jr.", "Odell Beckham", "Jr"),
        ("Odell Beckham Jr", "Odell Beckham", "Jr"),
        ("Robert Griffin III", "Robert Griffin", "III"),
        ("Marvin Harrison", "Marvin Harrison", None),
        # A one-word name that happens to *be* a suffix is not a suffix.
        ("V", "V", None),
    ],
)
def test_split_suffix(raw, base, suffix):
    assert split_suffix(raw) == (base, suffix)


def test_player_id_is_deterministic_and_prefixed():
    first = mint_player_id("sleeper", "2133")
    assert first.startswith("fdy-")
    assert len(first) == len("fdy-") + 12
    assert mint_player_id("sleeper", "2133") == first, (
        "a rebuild must re-mint the same id"
    )
    assert mint_player_id("sleeper", "2134") != first


def test_player_id_is_anchored_on_the_upstream_key_not_the_crosswalk():
    """Anchoring on "best available crosswalk id" would re-mint a player's id
    the day they finally gain a gsis_id, breaking the career-stability
    contract. The upstream's own key is present on every record, so it does
    not move."""
    assert mint_player_id("sleeper", "2133") != mint_player_id("gsis", "2133")


@pytest.mark.parametrize(
    ("position", "group"),
    [
        ("QB", "offense_skill"),
        ("WR", "offense_skill"),
        ("TE", "offense_skill"),
        ("C", "offense_line"),
        ("OT", "offense_line"),
        ("CB", "defense"),
        ("S", "defense"),
        ("LB", "defense"),
        ("K", "special_teams"),
        ("DST", "special_teams"),
        ("wr", "offense_skill"),
        ("NOT_A_POSITION", None),
        (None, None),
    ],
)
def test_position_group(position, group):
    assert position_group(position) == group


@pytest.mark.parametrize(
    ("status", "team", "expected"),
    [
        ("Active", "LV", "active"),
        ("Injured Reserve", "LV", "ir"),
        ("Practice Squad", "LV", "practice_squad"),
        ("Physically Unable to Perform", "LV", "pup"),
        ("Suspended", "LV", "suspended"),
        # Unknown/absent status: a team means a roster spot, no team means a
        # free agent. Nothing finer is invented.
        (None, "LV", "active"),
        (None, None, "free_agent"),
        ("Something New", None, "free_agent"),
    ],
)
def test_roster_status(status, team, expected):
    assert roster_status(status, team) == expected


def test_crosswalk_external_ids_records_adoption_not_a_score():
    """`match_score` stays null for an adopted link. Recording 1.0 would make
    a Tier-1 adoption indistinguishable from a perfect Tier-3 match in the
    miss analysis, which is exactly the comparison that matters."""
    record = {key: f"x-{key}" for key in CROSSWALK_KEYS}
    out = crosswalk_external_ids(record, NOW)

    assert set(out) == set(CROSSWALK_KEYS.values())
    for link in out.values():
        assert link["link_method"] == "crosswalk"
        assert link["match_score"] is None
        assert link["match_margin"] is None


def test_crosswalk_external_ids_skips_absent_sources():
    out = crosswalk_external_ids({"gsis_id": "00-1", "espn_id": None}, NOW)
    assert set(out) == {"gsis"}

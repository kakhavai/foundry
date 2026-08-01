"""The committed table, and the normalisations carried over from
`schedule_context/venues.py`.

That module's docstring calls itself transitional and names `venue` as what
replaces it. Two of its rules are load-bearing and are the reason this file
exists at all — losing either would produce numbers that pass every schema
check and are wrong:

1. A neutral-site game's venue is NOT the designated home club's stadium.
2. Zones are IANA, never fixed offsets.

The rest pins the honesty claims the module docstring makes about which fields
are populated. A docstring nothing checks is a docstring that drifts.
"""

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from venue import reference


def test_two_clubs_sharing_a_building_share_a_venue_id():
    """Which is what makes the trip between them zero miles."""
    assert reference._HOME_VENUE_IDS["NYG"] == reference._HOME_VENUE_IDS["NYJ"]
    assert reference._HOME_VENUE_IDS["LA"] == reference._HOME_VENUE_IDS["LAC"]
    assert len(reference._HOME_VENUE_IDS) == 32
    assert len(set(reference._HOME_VENUE_IDS.values())) == 30


def test_tenants_are_derived_from_the_club_map_not_written_twice():
    """A shared building must not be able to list one tenant here and two in
    the club map."""
    assert reference.HOME_TEAM_IDS["metlife"] == ("NYG", "NYJ")
    assert reference.HOME_TEAM_IDS["sofi"] == ("LA", "LAC")
    assert reference.HOME_TEAM_IDS["lambeau"] == ("GB",)
    # A neutral-only site has no tenants at all, and gets that for free.
    assert reference.REVISIONS["wembley"][0].home_team_ids == ()


def test_a_neutral_site_resolves_by_stadium_name_not_by_the_home_club():
    """The carried-over rule that matters most.

    The feed's `stadium_id` on a neutral row describes the DESIGNATED HOME
    CLUB's building; only the `stadium` name is correct. Reading Jacksonville's
    coordinates for a game played in Munich yields plausible, schema-valid
    numbers that are wrong by four thousand miles.
    """
    home = reference.resolve_venue_id(
        home_team="JAX", stadium_name="EverBank Stadium", is_neutral_site=False
    )
    away_in_munich = reference.resolve_venue_id(
        home_team="JAX", stadium_name="Allianz Arena", is_neutral_site=True
    )
    assert home == "everbank"
    assert away_in_munich == "allianz"
    assert home != away_in_munich


def test_an_unrecognised_neutral_stadium_resolves_to_nothing():
    """`None`, never a fallback. A guessed venue is the failure the coverage
    block exists to make visible; the caller records a miss with a reason."""
    assert (
        reference.resolve_venue_id(
            home_team="JAX",
            stadium_name="Somewhere Nobody Has Heard Of",
            is_neutral_site=True,
        )
        is None
    )


def test_every_spelling_the_feed_has_used_resolves_to_one_building():
    """The feed writes both "Tottenham Stadium" and "Tottenham Hotspur
    Stadium", and both "Estadio Azteca" and its sponsored name. An
    unrecognised spelling costs a whole row, so every observed one is listed.
    """
    pairs = (
        ("Tottenham Stadium", "Tottenham Hotspur Stadium"),
        ("Estadio Azteca", "Azteca Stadium"),
        ("Estadio Azteca", "Estadio Banorte"),
        ("Neo Química Arena", "Arena Corinthians"),
        ("Allianz Arena", "FC Bayern Munich Stadium"),
        ("Santiago Bernabéu", "Bernabeu"),
    )
    assert len(pairs) == 6
    for first, second in pairs:
        resolve = reference.resolve_venue_id
        assert resolve(
            home_team="JAX", stadium_name=first, is_neutral_site=True
        ) == resolve(home_team="JAX", stadium_name=second, is_neutral_site=True), (
            first,
            second,
        )


def test_accents_and_punctuation_fold_to_the_same_key():
    """Upstream spelling varies across seasons in ways that do not change which
    building it is."""
    assert reference.normalize_stadium("Estádio Azteca") == (
        reference.normalize_stadium("Estadio Azteca")
    )
    assert reference.normalize_stadium("Levi's Stadium") == (
        reference.normalize_stadium("levis stadium")
    )
    assert reference.normalize_stadium("  U.S. Bank Stadium  ") == "usbankstadium"


def test_a_neutral_alias_reuses_the_clubs_own_venue():
    """League buildings that also host neutral-site games map back to the
    club's venue_id rather than restating coordinates, so a correction to a
    building cannot leave its alias behind."""
    assert (
        reference.resolve_venue_id(
            home_team="TB", stadium_name="Levi's Stadium", is_neutral_site=True
        )
        == reference._HOME_VENUE_IDS["SF"]
    )
    # A former name of a building the club still plays in.
    assert (
        reference.resolve_venue_id(
            home_team="TB", stadium_name="FirstEnergy Stadium", is_neutral_site=True
        )
        == reference._HOME_VENUE_IDS["CLE"]
    )


def test_zones_are_iana_and_the_awkward_ones_are_right():
    """A fixed offset is wrong for exactly the games where these fields matter
    most: the season crosses the November DST transition, Arizona does not
    observe it, Indiana carries its own history, and Las Vegas keeps Pacific
    rather than Mountain time."""

    def zone(venue_id: str) -> str:
        return reference.REVISIONS[venue_id][0].record.timezone

    assert zone("state-farm") == "America/Phoenix"
    assert zone("lucas-oil") == "America/Indiana/Indianapolis"
    assert zone("allegiant") == "America/Los_Angeles"
    # Every zone must be loadable, not merely a plausible string: a typo'd zone
    # raises here rather than at 3am in a body-clock calculation.
    zones = [
        rev.record.timezone for revs in reference.REVISIONS.values() for rev in revs
    ]
    assert len(zones) >= 30
    for name in zones:
        ZoneInfo(name)


def test_country_is_not_assumed_to_be_us():
    countries = {
        rev.record.country for revs in reference.REVISIONS.values() for rev in revs
    }
    assert reference.LEAGUE_COUNTRY in countries
    assert {"GB", "DE", "MX", "BR"} <= countries, countries


# ── the honesty claims the module docstring makes ────────────────────────────


def _all_records():
    return [rev.record for revs in reference.REVISIONS.values() for rev in revs]


def test_the_fields_that_are_populated_everywhere_really_are():
    records = _all_records()
    assert len(records) >= 30
    for record in records:
        assert record.name and record.city and record.country
        assert record.timezone
        assert record.roof_type in reference.ROOF_TYPES
        assert -90 <= record.latitude <= 90
        assert -180 <= record.longitude <= 180


@pytest.mark.parametrize(
    "field_name",
    [
        "surface_product",
        "surface_installed_on",
        "surface_last_resurfaced_on",
        "roof_state_policy",
        "field_orientation_deg",
        "seating_capacity",
        "crowd_noise_profile",
        "year_built",
        "year_last_renovated",
    ],
)
def test_the_fields_documented_as_unsourced_are_null_everywhere(field_name):
    """ "Prefer None over a plausible-looking guess" is a claim, and this is
    what makes it checkable.

    If one of these is populated later, this test is what forces the module
    docstring to be updated in the same change rather than quietly becoming
    false.
    """
    records = _all_records()
    assert len(records) >= 30
    populated = [r.venue_id for r in records if getattr(r, field_name) is not None]
    assert populated == [], (
        f"{field_name} is documented as unsourced but is set on {populated}; "
        "either source it properly or update reference.py's docstring"
    )


# ── provenance: a POPULATED value must be falsifiable too ────────────────────
#
# Everything above this line asserts which fields are NULL. That is half an
# honesty claim, and the missing half let a real error through: `wembley`
# shipped as `retractable` because nothing in the table, the tests or CI could
# contradict a populated value. These tests are the other half. They are
# deliberately not exhaustive — the goal is that a wrong value is CATCHABLE,
# not that every value is proved.


def _roof(venue_id: str) -> str:
    return reference.REVISIONS[venue_id][0].record.roof_type


# Venues whose PITCH is open to the sky, whatever is over the stands. The rule
# `roof_type` encodes is "describes the pitch, not the building", and this is
# the list Wembley belonged on and was missing from. Several of these carry
# substantial spectator roofs — Wembley's sliding sections, Allianz Arena's
# membrane, the cantilevered rings at Stade de France, Maracanã, Croke Park and
# the MCG, Lumen Field's and Hard Rock's canopies — and none of them enclose
# the playing surface.
OPEN_PITCH_VENUES = (
    "wembley",
    "allianz",
    "stade-de-france",
    "maracana",
    "twickenham",
    "croke-park",
    "mcg",
    "tottenham",
    "azteca",
    "neo-quimica",
    "lumen",
    "hard-rock",
)

# Roofs that can close OVER THE PITCH.
TRUE_RETRACTABLES = (
    "att",
    "state-farm",
    "mercedes-benz",
    "nrg",
    "lucas-oil",
    "frankfurt",
    "bernabeu",
    "rogers-centre",
)

# Roofs permanently enclosing the pitch.
FIXED_DOMES = ("ford-field", "us-bank", "caesars-superdome", "allegiant", "sofi")


def test_roof_type_describes_the_pitch_not_the_seating_bowl():
    """The rule `wembley` was decided against wrongly.

    A roof over the stands leaves the playing surface outdoors, so the venue is
    `open` however much steel is overhead. Getting this wrong is worse than a
    null: `weather` resolves `environment` from it BEFORE populating any
    meteorological field, so a mis-set venue produces a confident forecast for
    the wrong kind of place.
    """
    assert len(OPEN_PITCH_VENUES) == 12
    wrong = {v: _roof(v) for v in OPEN_PITCH_VENUES if _roof(v) != "open"}
    assert wrong == {}, (
        f"{wrong} have roofs over the seating bowl only; roof_type describes the PITCH"
    )


def test_the_true_retractables_and_fixed_domes_are_pinned():
    """The other direction. Without this, `open` everywhere would pass the test
    above perfectly."""
    assert len(TRUE_RETRACTABLES) == 8
    assert len(FIXED_DOMES) == 5
    assert {v: _roof(v) for v in TRUE_RETRACTABLES} == dict.fromkeys(
        TRUE_RETRACTABLES, "retractable"
    )
    assert {v: _roof(v) for v in FIXED_DOMES} == dict.fromkeys(
        FIXED_DOMES, "fixed_dome"
    )


def test_every_neutral_site_venue_is_open_or_a_pinned_retractable():
    """No neutral-site venue may quietly acquire a roof classification that
    neither list above covers — which is exactly how `wembley` drifted."""
    club_venues = set(reference._HOME_VENUE_IDS.values())
    neutral = sorted(set(reference.REVISIONS) - club_venues)
    assert len(neutral) == 13
    unaccounted = [
        v for v in neutral if v not in OPEN_PITCH_VENUES and v not in TRUE_RETRACTABLES
    ]
    assert unaccounted == [], (
        f"{unaccounted} carry an unreviewed roof classification; add each to "
        "OPEN_PITCH_VENUES or TRUE_RETRACTABLES with a provenance note"
    )


def test_a_handful_of_surface_classifications_are_pinned():
    """`surface_class` is the weaker of the two judgement fields, but an
    unfalsifiable value is still unfalsifiable. These are the ones this table
    is most confident about: a fixed dome is not natural grass without a
    retractable tray, and Lambeau's hybrid system is long-standing."""

    def surface(venue_id: str) -> str | None:
        return reference.REVISIONS[venue_id][0].record.surface_class

    pinned = {
        "lambeau": "hybrid",
        "ford-field": "synthetic_turf",
        "us-bank": "synthetic_turf",
        "caesars-superdome": "synthetic_turf",
        "metlife": "synthetic_turf",
        "lincoln-financial": "natural_grass",
        "arrowhead": "natural_grass",
        "empower-field": "natural_grass",
    }
    assert len(pinned) == 8
    assert {v: surface(v) for v in pinned} == pinned


def test_altitude_is_populated_only_where_it_is_unambiguous_and_matters():
    records = _all_records()
    with_altitude = {r.venue_id: r.altitude_ft for r in records if r.altitude_ft}
    assert with_altitude == {"empower-field": 5280, "azteca": 7280}


def test_surface_class_is_null_exactly_where_documented():
    """Null on the two league buildings with a recent change this table cannot
    confirm, and on every neutral site, whose pitch is re-laid per event."""
    club_venues = set(reference._HOME_VENUE_IDS.values())
    null_clubs = {
        rev.record.venue_id
        for venue_id, revs in reference.REVISIONS.items()
        for rev in revs
        if venue_id in club_venues and rev.record.surface_class is None
    }
    assert null_clubs == {"gillette", "highmark"}, null_clubs

    neutral_with_surface = {
        venue_id
        for venue_id, revs in reference.REVISIONS.items()
        if venue_id not in club_venues
        and any(rev.record.surface_class is not None for rev in revs)
    }
    assert neutral_with_surface == set()


def test_an_invalid_enum_value_fails_at_construction():
    """Validated at import rather than at capture time: a typo in a committed
    table should fail the process that loads it, not produce one schema-invalid
    row six weeks into a season."""
    with pytest.raises(ValueError, match="roof_type"):
        reference.VenueRecord(
            venue_id="x",
            effective_from=date(2026, 1, 1),
            name="X",
            city="X",
            country="US",
            latitude=0.0,
            longitude=0.0,
            timezone="UTC",
            roof_type="sliding",
        )
    with pytest.raises(ValueError, match="roof_state_policy"):
        reference.VenueRecord(
            venue_id="x",
            effective_from=date(2026, 1, 1),
            name="X",
            city="X",
            country="US",
            latitude=0.0,
            longitude=0.0,
            timezone="UTC",
            roof_type="retractable",
            roof_state_policy="sometimes",
        )
    with pytest.raises(ValueError, match="surface_class"):
        reference.VenueRecord(
            venue_id="x",
            effective_from=date(2026, 1, 1),
            name="X",
            city="X",
            country="US",
            latitude=0.0,
            longitude=0.0,
            timezone="UTC",
            roof_type="open",
            surface_class="astroturf",
        )


# ── home-field advantage classification ──────────────────────────────────────


def _revision(venue_id: str):
    return reference.REVISIONS[venue_id][0]


def test_hfa_normal_for_an_ordinary_home_game():
    assert (
        reference.home_field_advantage_class(
            _revision("lambeau"), designated_home_team_id="GB", is_neutral_site=False
        )
        == reference.HFA_NORMAL
    )


def test_hfa_shared_venue_for_both_metlife_tenants():
    """The spec names this case explicitly, and it is why the classification
    cannot be derived from the club alone."""
    for club in ("NYG", "NYJ"):
        assert (
            reference.home_field_advantage_class(
                _revision("metlife"),
                designated_home_team_id=club,
                is_neutral_site=False,
            )
            == reference.HFA_SHARED_VENUE
        )


def test_hfa_international_outranks_neutral():
    """Every international game is also neutral, and the longer answer is the
    more useful one."""
    assert (
        reference.home_field_advantage_class(
            _revision("allianz"), designated_home_team_id="JAX", is_neutral_site=True
        )
        == reference.HFA_INTERNATIONAL
    )


def test_hfa_neutral_outranks_shared_venue():
    """A neutral-site game at a building that has tenants confers no home
    advantage on either participant, even though the building is shared."""
    assert (
        reference.home_field_advantage_class(
            _revision("metlife"), designated_home_team_id="NYG", is_neutral_site=True
        )
        == reference.HFA_NEUTRAL
    )


def test_every_classification_is_one_of_the_declared_four():
    cases = (
        (_revision("lambeau"), "GB", False),
        (_revision("metlife"), "NYJ", False),
        (_revision("metlife"), "NYG", True),
        (_revision("wembley"), "JAX", True),
    )
    assert len(cases) == 4
    results = {
        reference.home_field_advantage_class(
            revision, designated_home_team_id=club, is_neutral_site=neutral
        )
        for revision, club, neutral in cases
    }
    assert results == reference.HFA_CLASSES

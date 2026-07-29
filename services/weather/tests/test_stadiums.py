from weather.stadiums import BY_STADIUM_ID, RETRACTABLE_STADIUM_IDS, STADIUMS

VALID_ROOF_TYPES = {"open", "fixed_dome", "retractable"}
VALID_ENCLOSURE = {"exposed", "partial", "enclosed"}


def test_every_stadium_has_the_new_fields():
    for slug, stadium in STADIUMS.items():
        assert stadium["stadium_id"], f"{slug} is missing stadium_id"
        assert stadium["roof_type"] in VALID_ROOF_TYPES, slug
        assert stadium["enclosure_class"] in VALID_ENCLOSURE, slug


def test_stadium_ids_are_unique():
    ids = [s["stadium_id"] for s in STADIUMS.values()]
    assert len(ids) == len(set(ids))


def test_lookup_by_stadium_id_round_trips():
    for stadium in STADIUMS.values():
        assert BY_STADIUM_ID[stadium["stadium_id"]] is stadium


def test_the_five_retractable_venues_are_marked():
    """These are exactly the ids the schedule feed leaves roof empty for.
    If this set drifts, an empty roof stops being distinguishable from a gap."""
    assert RETRACTABLE_STADIUM_IDS == {"ATL97", "DAL00", "HOU00", "IND00", "PHO00"}


def test_retractable_set_agrees_with_the_table():
    from_table = {
        s["stadium_id"] for s in STADIUMS.values() if s["roof_type"] == "retractable"
    }
    assert from_table == RETRACTABLE_STADIUM_IDS


def test_coordinates_are_plausible():
    for slug, stadium in STADIUMS.items():
        assert -90 <= stadium["latitude"] <= 90, slug
        assert -180 <= stadium["longitude"] <= 180, slug

from datetime import UTC, datetime

import pytest

from weather.adapters.schedule import ScheduledGame
from weather.environment import (
    Environment,
    UnresolvableVenue,
    resolve_environment,
    resolve_venue,
)
from weather.stadiums import BY_STADIUM_ID

KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def game(**overrides) -> ScheduledGame:
    base = dict(
        game_id="2026_01_CHI_CAR",
        season=2026,
        week=1,
        kickoff_at=KICKOFF,
        home_team="CAR",
        away_team="CHI",
        stadium_id="CAR00",
        stadium_name="Bank of America Stadium",
        is_neutral_site=False,
        roof_raw="outdoors",
    )
    return ScheduledGame(**{**base, **overrides})


def test_outdoors_maps_to_outdoor():
    g = game()
    assert resolve_environment(g, resolve_venue(g)) is Environment.OUTDOOR


def test_dome_maps_to_fixed_dome():
    dome_id = next(
        s["stadium_id"]
        for s in BY_STADIUM_ID.values()
        if s["roof_type"] == "fixed_dome"
    )
    g = game(stadium_id=dome_id, roof_raw="dome")
    assert resolve_environment(g, resolve_venue(g)) is Environment.FIXED_DOME


def test_open_and_closed_map_to_retractable_states():
    g_open = game(stadium_id="HOU00", roof_raw="open")
    g_closed = game(stadium_id="HOU00", roof_raw="closed")
    assert (
        resolve_environment(g_open, resolve_venue(g_open))
        is Environment.RETRACTABLE_OPEN
    )
    assert (
        resolve_environment(g_closed, resolve_venue(g_closed))
        is Environment.RETRACTABLE_CLOSED
    )


def test_empty_roof_at_a_retractable_venue_is_undecided():
    """The honest answer on a Wednesday for a Sunday game. Not a data gap."""
    g = game(stadium_id="HOU00", roof_raw=None)
    assert resolve_environment(g, resolve_venue(g)) is Environment.RETRACTABLE_UNDECIDED


def test_empty_roof_at_a_non_retractable_venue_is_a_real_gap():
    """Only retractables legitimately lack a roof value. Anywhere else it
    means the feed broke, and guessing would hide that."""
    g = game(stadium_id="CAR00", roof_raw=None)
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_environment(g, resolve_venue(g))
    assert exc.value.reason == "missing_roof_state"


def test_neutral_site_without_a_table_entry_is_refused():
    """Never falls back to the designated home team's venue."""
    g = game(
        game_id="2026_10_NE_DET",
        stadium_id=None,
        stadium_name="FC Bayern Munich Stadium",
        is_neutral_site=True,
        roof_raw=None,
    )
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_venue(g)
    assert exc.value.reason == "neutral_site_venue_unknown"


def test_unknown_stadium_id_is_refused():
    g = game(stadium_id="ZZZ99")
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_venue(g)
    assert exc.value.reason == "unknown_stadium_id"


def test_unrecognised_roof_value_is_refused():
    g = game(roof_raw="partially_ajar")
    with pytest.raises(UnresolvableVenue) as exc:
        resolve_environment(g, resolve_venue(g))
    assert exc.value.reason == "unrecognised_roof_value"

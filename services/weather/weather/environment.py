"""Resolve the playing environment before any meteorological field is populated.

An outdoor forecast for a closed dome is not merely wrong, it is confidently
wrong — plausible temperature, plausible wind, and no way to tell from the
record. So environment is resolved first, and anything unresolvable refuses
rather than guesses.
"""

from enum import StrEnum

from .adapters.schedule import ScheduledGame
from .stadiums import BY_STADIUM_ID, RETRACTABLE_STADIUM_IDS


class Environment(StrEnum):
    OUTDOOR = "outdoor"
    FIXED_DOME = "fixed_dome"
    RETRACTABLE_OPEN = "retractable_open"
    RETRACTABLE_CLOSED = "retractable_closed"
    RETRACTABLE_UNDECIDED = "retractable_undecided"


# Verified against the live schedule feed, 2026-07-29.
_ROOF_MAP = {
    "outdoors": Environment.OUTDOOR,
    "dome": Environment.FIXED_DOME,
    "open": Environment.RETRACTABLE_OPEN,
    "closed": Environment.RETRACTABLE_CLOSED,
}

# Meteorological fields are null under these — the sky is not a factor.
IS_CLOSED_ENVIRONMENT = frozenset(
    {Environment.FIXED_DOME, Environment.RETRACTABLE_CLOSED}
)


class UnresolvableVenue(Exception):
    """The venue or its roof state cannot be determined. The caller must count
    the game in `coverage.missing` rather than emit a record."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def resolve_venue(game: ScheduledGame) -> dict:
    """The venue record for a game, or refuse.

    A neutral-site game has no usable `stadium_id` — the adapter discarded the
    feed's value because it names the designated home team's stadium. Until the
    international venues are added to the table, these refuse. They are never
    resolved to the home team's stadium, which is the failure this guards.
    """
    if game.stadium_id is None:
        raise UnresolvableVenue(
            "neutral_site_venue_unknown" if game.is_neutral_site else "no_stadium_id",
            game.stadium_name,
        )
    venue = BY_STADIUM_ID.get(game.stadium_id)
    if venue is None:
        raise UnresolvableVenue("unknown_stadium_id", game.stadium_id)
    return venue


def resolve_environment(game: ScheduledGame, venue: dict) -> Environment:
    if game.roof_raw is None:
        # Only a retractable legitimately has no roof state before kickoff.
        # Anywhere else, the absence means the feed broke.
        if venue["stadium_id"] in RETRACTABLE_STADIUM_IDS:
            return Environment.RETRACTABLE_UNDECIDED
        raise UnresolvableVenue("missing_roof_state", game.game_id)

    resolved = _ROOF_MAP.get(game.roof_raw)
    if resolved is None:
        raise UnresolvableVenue("unrecognised_roof_value", game.roof_raw)
    return resolved

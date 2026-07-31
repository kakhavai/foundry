"""Venue coordinates and IANA zones — transitional, exactly like weather's
bundled schedule adapter is transitional.

The phase-8 spec for this collector says travel and body-clock fields
"require venue coordinates and IANA zones rather than city strings", sourced
"from the `venue` collector rather than a third party". `venue` is an 8E
collector and does not exist yet, so this module stands in for it. The
`Venue` interface is the seam: when `venue` lands, `resolve_venue` becomes an
HTTP call and nothing above it changes.

It is deliberately *not* in `collector-core`. A venue table is `venue`'s job,
not the shared library's, and moving it into the library now would mean
deleting it from there when the real collector arrives.

Two normalisations here are load-bearing:

1. **A neutral-site game's venue is not the home team's stadium.** The
   upstream's `stadium_id` and `roof` describe the DESIGNATED HOME TEAM's
   stadium for those rows; only the `stadium` name is correct. Reading the
   home team's coordinates for a game played in Munich yields plausible
   numbers that pass every schema check and are wrong by four thousand
   miles. Neutral rows therefore resolve by stadium NAME, and an
   unrecognised name resolves to nothing rather than to a guess.

2. **Zones are IANA, never fixed offsets.** The season crosses the November
   DST transition, and `America/Phoenix` does not observe DST at all while
   `America/Denver` does — so Arizona's offset relative to the rest of the
   Mountain zone changes mid-season. A fixed offset is wrong for exactly the
   games where the timezone fields matter most.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# The league's home country. `is_international` is "venue country differs from
# the league's home country", so this is the thing it differs from.
LEAGUE_COUNTRY = "US"


@dataclass(frozen=True)
class Venue:
    """One place a game is played. The interface `venue` (8E) will supply."""

    venue_id: str
    name: str
    latitude: float
    longitude: float
    timezone: str
    country: str

    def utc_offset_hours(self, instant: datetime) -> float:
        """The venue's UTC offset, in hours, at a specific instant.

        Takes the instant rather than being a constant because DST is the
        whole reason this is a zone and not a number.
        """
        if instant.tzinfo is None:
            raise ValueError(
                f"timezone-aware datetime required to resolve an offset, "
                f"got naive: {instant!r}"
            )
        offset = instant.astimezone(ZoneInfo(self.timezone)).utcoffset()
        # `utcoffset()` is None only for a naive datetime, which is rejected
        # above; the guard keeps the type checker and the reader honest.
        assert offset is not None
        return offset.total_seconds() / 3600.0

    def local_time(self, instant: datetime) -> str:
        """Wall-clock `HH:MM:SS` at the venue for a UTC instant."""
        if instant.tzinfo is None:
            raise ValueError(
                f"timezone-aware datetime required for a local time, "
                f"got naive: {instant!r}"
            )
        return instant.astimezone(ZoneInfo(self.timezone)).strftime("%H:%M:%S")


def _venue(
    venue_id: str,
    name: str,
    latitude: float,
    longitude: float,
    timezone: str,
    country: str = LEAGUE_COUNTRY,
) -> Venue:
    return Venue(venue_id, name, latitude, longitude, timezone, country)


# Each club's home venue, keyed by the upstream's team abbreviation. Two pairs
# share a building (the two Los Angeles clubs, the two New York clubs), which
# is why the table is keyed by team and the *venue* carries its own id: two
# teams with the same `venue_id` travel zero miles to play each other.
TEAM_VENUES: dict[str, Venue] = {
    "ARI": _venue("state-farm", "State Farm Stadium", 33.5276, -112.2626, "America/Phoenix"),  # noqa: E501
    "ATL": _venue("mercedes-benz", "Mercedes-Benz Stadium", 33.7554, -84.4008, "America/New_York"),  # noqa: E501
    "BAL": _venue("mt-bank", "M&T Bank Stadium", 39.2780, -76.6227, "America/New_York"),  # noqa: E501
    "BUF": _venue("highmark", "Highmark Stadium", 42.7738, -78.7870, "America/New_York"),  # noqa: E501
    "CAR": _venue("bank-of-america", "Bank of America Stadium", 35.2258, -80.8528, "America/New_York"),  # noqa: E501
    "CHI": _venue("soldier-field", "Soldier Field", 41.8623, -87.6167, "America/Chicago"),  # noqa: E501
    "CIN": _venue("paycor", "Paycor Stadium", 39.0955, -84.5161, "America/New_York"),  # noqa: E501
    "CLE": _venue("huntington-bank-field", "Huntington Bank Field", 41.5061, -81.6995, "America/New_York"),  # noqa: E501
    "DAL": _venue("att", "AT&T Stadium", 32.7473, -97.0945, "America/Chicago"),
    "DEN": _venue("empower-field", "Empower Field at Mile High", 39.7439, -105.0201, "America/Denver"),  # noqa: E501
    "DET": _venue("ford-field", "Ford Field", 42.3400, -83.0456, "America/Detroit"),  # noqa: E501
    "GB": _venue("lambeau", "Lambeau Field", 44.5013, -88.0622, "America/Chicago"),
    "HOU": _venue("nrg", "NRG Stadium", 29.6847, -95.4107, "America/Chicago"),
    # America/Indiana/Indianapolis, not America/New_York: Indiana did not
    # observe DST until 2006, and the zone carries that history.
    "IND": _venue("lucas-oil", "Lucas Oil Stadium", 39.7601, -86.1639, "America/Indiana/Indianapolis"),  # noqa: E501
    "JAX": _venue("everbank", "EverBank Stadium", 30.3239, -81.6373, "America/New_York"),  # noqa: E501
    "KC": _venue("arrowhead", "Arrowhead Stadium", 39.0489, -94.4839, "America/Chicago"),  # noqa: E501
    "LA": _venue("sofi", "SoFi Stadium", 33.9535, -118.3392, "America/Los_Angeles"),
    "LAC": _venue("sofi", "SoFi Stadium", 33.9535, -118.3392, "America/Los_Angeles"),
    "LV": _venue("allegiant", "Allegiant Stadium", 36.0909, -115.1833, "America/Los_Angeles"),  # noqa: E501
    "MIA": _venue("hard-rock", "Hard Rock Stadium", 25.9580, -80.2389, "America/New_York"),  # noqa: E501
    "MIN": _venue("us-bank", "U.S. Bank Stadium", 44.9736, -93.2575, "America/Chicago"),  # noqa: E501
    "NE": _venue("gillette", "Gillette Stadium", 42.0909, -71.2643, "America/New_York"),  # noqa: E501
    "NO": _venue("caesars-superdome", "Caesars Superdome", 29.9511, -90.0812, "America/Chicago"),  # noqa: E501
    "NYG": _venue("metlife", "MetLife Stadium", 40.8135, -74.0745, "America/New_York"),  # noqa: E501
    "NYJ": _venue("metlife", "MetLife Stadium", 40.8135, -74.0745, "America/New_York"),  # noqa: E501
    "PHI": _venue("lincoln-financial", "Lincoln Financial Field", 39.9008, -75.1675, "America/New_York"),  # noqa: E501
    "PIT": _venue("acrisure", "Acrisure Stadium", 40.4468, -80.0158, "America/New_York"),  # noqa: E501
    "SEA": _venue("lumen", "Lumen Field", 47.5952, -122.3316, "America/Los_Angeles"),  # noqa: E501
    "SF": _venue("levis", "Levi's Stadium", 37.4033, -121.9694, "America/Los_Angeles"),  # noqa: E501
    "TB": _venue("raymond-james", "Raymond James Stadium", 27.9759, -82.5033, "America/New_York"),  # noqa: E501
    "TEN": _venue("nissan", "Nissan Stadium", 36.1665, -86.7713, "America/Chicago"),
    "WAS": _venue("northwest", "Northwest Stadium", 38.9077, -76.8645, "America/New_York"),  # noqa: E501
}

# Venues used for neutral-site games, keyed by the NORMALISED upstream stadium
# name. Keyed by name because the upstream's `stadium_id` for a neutral row
# points at the designated home team's building, not where the game is played.
NEUTRAL_VENUES: dict[str, Venue] = {
    "wembleystadium": _venue("wembley", "Wembley Stadium", 51.5560, -0.2795, "Europe/London", "GB"),  # noqa: E501
    "tottenhamhotspurstadium": _venue("tottenham", "Tottenham Hotspur Stadium", 51.6043, -0.0665, "Europe/London", "GB"),  # noqa: E501
    "allianzarena": _venue("allianz", "Allianz Arena", 48.2188, 11.6247, "Europe/Berlin", "DE"),  # noqa: E501
    "deutschebankpark": _venue("deutsche-bank-park", "Deutsche Bank Park", 50.0685, 8.6455, "Europe/Berlin", "DE"),  # noqa: E501
    "estadioazteca": _venue("azteca", "Estadio Azteca", 19.3029, -99.1505, "America/Mexico_City", "MX"),  # noqa: E501
    "neoquimicaarena": _venue("neo-quimica", "Neo Quimica Arena", -23.5453, -46.4742, "America/Sao_Paulo", "BR"),  # noqa: E501
    "crokepark": _venue("croke-park", "Croke Park", 53.3607, -6.2512, "Europe/Dublin", "IE"),  # noqa: E501
    "santiagobernabeu": _venue("bernabeu", "Santiago Bernabeu", 40.4531, -3.6883, "Europe/Madrid", "ES"),  # noqa: E501
}

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

# Accented characters the upstream spells inconsistently across seasons
# ("Estádio Azteca", "Neo Química Arena"). Folded before the alphanumeric
# squeeze so both spellings land on one key.
_FOLD = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüçñ", "aaaaaeeeeiiiiooooouuuucn")


def normalize_stadium(name: str) -> str:
    """Fold a stadium name to its lookup key.

    Upstream spelling varies across seasons in punctuation, capitalisation and
    accents, none of which change which building it is.
    """
    return _NON_ALPHANUMERIC.sub("", name.strip().lower().translate(_FOLD))


def resolve_venue(*, home_team: str, stadium_name: str, is_neutral_site: bool) -> Venue | None:
    """Where a game is actually played, or `None` if that cannot be determined.

    `None` rather than a fallback on purpose. A guessed venue produces travel,
    timezone and body-clock numbers that are plausible, schema-valid and
    wrong — which is precisely the failure the coverage block exists to make
    visible. The caller records the row as missing with a reason instead.
    """
    if is_neutral_site:
        return NEUTRAL_VENUES.get(normalize_stadium(stadium_name))
    return TEAM_VENUES.get(home_team)


def home_venue(team: str) -> Venue | None:
    """The team's own venue — the zone its body clock is set to."""
    return TEAM_VENUES.get(team)

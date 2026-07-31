"""Venue coordinates and IANA zones — transitional, exactly like weather's
bundled schedule adapter is transitional.

The phase-8 spec for this collector says travel and body-clock fields
"require venue coordinates and IANA zones rather than city strings", sourced
"from the `venue` collector rather than a third party". `venue` is an 8E
collector and does not exist yet, so this module stands in for it. `Venue` is
the seam: when `venue` lands, `resolve_venue` becomes a call to it and nothing
above this module changes.

It is deliberately **not** in `collector-core`. A venue table is `venue`'s job,
not the shared library's, and moving it into the library now would only mean
deleting it from there when the real collector arrives.

Two normalisations here are load-bearing:

1. **A neutral-site game's venue is not the home club's stadium.** The
   upstream's `stadium_id` describes the DESIGNATED HOME CLUB's stadium for
   those rows; only the `stadium` name is correct. Reading the home club's
   coordinates for a game played in Munich yields plausible numbers that pass
   every schema check and are wrong by four thousand miles. Neutral rows
   therefore resolve by stadium NAME, and an unrecognised name resolves to
   nothing rather than to a guess.

2. **Zones are IANA, never fixed offsets.** The season crosses the November DST
   transition, and `America/Phoenix` does not observe DST at all while
   `America/Denver` does — so Arizona's offset relative to the rest of the
   Mountain zone changes mid-season. A fixed offset is wrong for exactly the
   games where these fields matter most.
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
    """One place a game is played. The interface `venue` (8E) will supply.

    No display name: nothing this collector emits carries one, and `venue_id`
    is the identity a consumer joins on. Two clubs sharing a building share a
    `venue_id`, which is what makes the trip between them zero miles.
    """

    venue_id: str
    latitude: float
    longitude: float
    timezone: str
    country: str = LEAGUE_COUNTRY

    def _local(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            raise ValueError(
                f"timezone-aware datetime required, got naive: {instant!r}"
            )
        return instant.astimezone(ZoneInfo(self.timezone))

    def utc_offset_hours(self, instant: datetime) -> float:
        """The venue's UTC offset in hours, at a specific instant.

        Takes the instant rather than being a constant because DST is the whole
        reason this is a zone and not a number.
        """
        offset = self._local(instant).utcoffset()
        # `utcoffset()` is None only for a naive datetime, which `_local`
        # already rejected; the guard keeps the reader honest.
        assert offset is not None
        return offset.total_seconds() / 3600.0

    def local_time(self, instant: datetime) -> str:
        """Wall-clock `HH:MM:SS` at the venue for a UTC instant."""
        return self._local(instant).strftime("%H:%M:%S")


# Each club's home venue: club -> (venue_id, latitude, longitude, IANA zone).
# Two pairs share a building — the two Los Angeles clubs and the two New York
# clubs — which is why the venue id is data rather than the club's own name.
_HOME_VENUES: dict[str, tuple[str, float, float, str]] = {
    "ARI": ("state-farm", 33.5276, -112.2626, "America/Phoenix"),
    "ATL": ("mercedes-benz", 33.7554, -84.4008, "America/New_York"),
    "BAL": ("mt-bank", 39.2780, -76.6227, "America/New_York"),
    "BUF": ("highmark", 42.7738, -78.7870, "America/New_York"),
    "CAR": ("bank-of-america", 35.2258, -80.8528, "America/New_York"),
    "CHI": ("soldier-field", 41.8623, -87.6167, "America/Chicago"),
    "CIN": ("paycor", 39.0955, -84.5161, "America/New_York"),
    "CLE": ("huntington-bank-field", 41.5061, -81.6995, "America/New_York"),
    "DAL": ("att", 32.7473, -97.0945, "America/Chicago"),
    "DEN": ("empower-field", 39.7439, -105.0201, "America/Denver"),
    "DET": ("ford-field", 42.3400, -83.0456, "America/Detroit"),
    "GB": ("lambeau", 44.5013, -88.0622, "America/Chicago"),
    "HOU": ("nrg", 29.6847, -95.4107, "America/Chicago"),
    # America/Indiana/Indianapolis, not America/New_York: Indiana did not
    # observe DST until 2006 and the zone carries that history.
    "IND": ("lucas-oil", 39.7601, -86.1639, "America/Indiana/Indianapolis"),
    "JAX": ("everbank", 30.3239, -81.6373, "America/New_York"),
    "KC": ("arrowhead", 39.0489, -94.4839, "America/Chicago"),
    "LA": ("sofi", 33.9535, -118.3392, "America/Los_Angeles"),
    "LAC": ("sofi", 33.9535, -118.3392, "America/Los_Angeles"),
    # Las Vegas keeps Pacific time, not Mountain.
    "LV": ("allegiant", 36.0909, -115.1833, "America/Los_Angeles"),
    "MIA": ("hard-rock", 25.9580, -80.2389, "America/New_York"),
    "MIN": ("us-bank", 44.9736, -93.2575, "America/Chicago"),
    "NE": ("gillette", 42.0909, -71.2643, "America/New_York"),
    "NO": ("caesars-superdome", 29.9511, -90.0812, "America/Chicago"),
    "NYG": ("metlife", 40.8135, -74.0745, "America/New_York"),
    "NYJ": ("metlife", 40.8135, -74.0745, "America/New_York"),
    "PHI": ("lincoln-financial", 39.9008, -75.1675, "America/New_York"),
    "PIT": ("acrisure", 40.4468, -80.0158, "America/New_York"),
    "SEA": ("lumen", 47.5952, -122.3316, "America/Los_Angeles"),
    "SF": ("levis", 37.4033, -121.9694, "America/Los_Angeles"),
    "TB": ("raymond-james", 27.9759, -82.5033, "America/New_York"),
    "TEN": ("nissan", 36.1665, -86.7713, "America/Chicago"),
    "WAS": ("northwest", 38.9077, -76.8645, "America/New_York"),
}

# Venues outside the league's own buildings, keyed by the upstream's stadium
# NAME. Keyed by name because `stadium_id` on a neutral row is unreliable: the
# feed has spelled Wembley's as both LON00 and JAX00, and Tottenham's as WAS00,
# because for some rows it carries the DESIGNATED HOME CLUB's building instead
# of the venue.
#
# Several buildings appear under more than one name — the feed writes both
# "Tottenham Stadium" and "Tottenham Hotspur Stadium", and both "Estadio
# Azteca" and its sponsored name "Estadio Banorte". Every spelling the feed has
# actually used is listed, because an unrecognised one costs a whole
# situational row. `test_every_neutral_site_name_the_feed_uses_resolves` pins
# the list against the names observed in the real document.
_NEUTRAL_VENUES: dict[str, tuple[str, float, float, str, str]] = {
    "Wembley Stadium": ("wembley", 51.5560, -0.2795, "Europe/London", "GB"),
    "Twickenham Stadium": ("twickenham", 51.4560, -0.3415, "Europe/London", "GB"),
    "Tottenham Stadium": ("tottenham", 51.6043, -0.0665, "Europe/London", "GB"),
    "Tottenham Hotspur Stadium": ("tottenham", 51.6043, -0.0665, "Europe/London", "GB"),
    "Allianz Arena": ("allianz", 48.2188, 11.6247, "Europe/Berlin", "DE"),
    "FC Bayern Munich Stadium": ("allianz", 48.2188, 11.6247, "Europe/Berlin", "DE"),
    "Deutsche Bank Park": ("frankfurt", 50.0685, 8.6455, "Europe/Berlin", "DE"),
    "Estadio Azteca": ("azteca", 19.3029, -99.1505, "America/Mexico_City", "MX"),
    "Azteca Stadium": ("azteca", 19.3029, -99.1505, "America/Mexico_City", "MX"),
    "Estadio Banorte": ("azteca", 19.3029, -99.1505, "America/Mexico_City", "MX"),
    "Neo Química Arena": ("neo-quimica", -23.5453, -46.4742, "America/Sao_Paulo", "BR"),
    "Arena Corinthians": ("neo-quimica", -23.5453, -46.4742, "America/Sao_Paulo", "BR"),
    "Maracana Stadium": ("maracana", -22.9121, -43.2302, "America/Sao_Paulo", "BR"),
    "Croke Park": ("croke-park", 53.3607, -6.2512, "Europe/Dublin", "IE"),
    "Santiago Bernabéu": ("bernabeu", 40.4531, -3.6883, "Europe/Madrid", "ES"),
    "Bernabeu": ("bernabeu", 40.4531, -3.6883, "Europe/Madrid", "ES"),
    "Stade de France": ("stade-de-france", 48.9245, 2.3601, "Europe/Paris", "FR"),
    "Melbourne Cricket Ground": (
        "mcg",
        -37.8200,
        144.9834,
        "Australia/Melbourne",
        "AU",
    ),
    "Rogers Centre": ("rogers-centre", 43.6414, -79.3894, "America/Toronto", "CA"),
}

# League buildings that also host neutral-site games — a Super Bowl, a
# hurricane relocation, an international game moved home. These reuse the home
# venue's coordinates rather than restating them, so a correction to a club's
# location cannot leave its neutral alias behind. Keyed by the feed's stadium
# name; several are former names of a building the club still plays in, which
# is exactly the drift a name lookup has to survive.
_NEUTRAL_ALIASES: dict[str, str] = {
    "State Farm Stadium": "ARI",
    "University of Phoenix Stadium": "ARI",
    "Mercedes-Benz Stadium": "ATL",
    "Huntington Bank Field": "CLE",
    "FirstEnergy Stadium": "CLE",
    "AT&T Stadium": "DAL",
    "Cowboys Stadium": "DAL",
    "Ford Field": "DET",
    "NRG Stadium": "HOU",
    "Reliant Stadium": "HOU",
    "Lucas Oil Stadium": "IND",
    "EverBank Stadium": "JAX",
    "TIAA Bank Stadium": "JAX",
    "Alltel Stadium": "JAX",
    "SoFi Stadium": "LA",
    "Allegiant Stadium": "LV",
    "Hard Rock Stadium": "MIA",
    "Dolphin Stadium": "MIA",
    "U.S. Bank Stadium": "MIN",
    "Caesars Superdome": "NO",
    "Mercedes-Benz Superdome": "NO",
    "Louisiana Superdome": "NO",
    "MetLife Stadium": "NYG",
    "Acrisure Stadium": "PIT",
    "Levi's Stadium": "SF",
    "Raymond James Stadium": "TB",
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


TEAM_VENUES: dict[str, Venue] = {
    team: Venue(venue_id, latitude, longitude, timezone)
    for team, (venue_id, latitude, longitude, timezone) in _HOME_VENUES.items()
}

NEUTRAL_VENUES: dict[str, Venue] = {
    **{
        normalize_stadium(name): Venue(*fields)
        for name, fields in _NEUTRAL_VENUES.items()
    },
    **{
        normalize_stadium(name): TEAM_VENUES[team]
        for name, team in _NEUTRAL_ALIASES.items()
    },
}


def resolve_venue(
    *, home_team: str, stadium_name: str, is_neutral_site: bool
) -> Venue | None:
    """Where a game is actually played, or `None` if that cannot be determined.

    `None` rather than a fallback on purpose. A guessed venue produces travel,
    timezone and body-clock numbers that are plausible, schema-valid and wrong
    — which is precisely the failure the coverage block exists to make visible.
    The caller records the row as missing with a reason instead.
    """
    if is_neutral_site:
        return NEUTRAL_VENUES.get(normalize_stadium(stadium_name))
    return TEAM_VENUES.get(home_team)


def home_venue(team: str) -> Venue | None:
    """The club's own venue — the zone its body clock is set to."""
    return TEAM_VENUES.get(team)

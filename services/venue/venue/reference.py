"""The committed venue reference table, and the revision machinery over it.

This is `venue`'s upstream. The phase-8 spec names four candidate sources —
"a hand-maintained reference table committed alongside the adapter, nflverse
stadium tables, Wikidata venue entities, club facility pages" — and this
collector is built on the first, deliberately: it reaches no third party for
the venue records themselves, so `venue_static` is fully deterministic and a
test can assert real captured output rather than a mock's.

It **supersedes** `services/schedule-context/schedule_context/venues.py`, whose
own docstring says it stands in for this collector until it exists. That module
is not rewired here — that is a follow-up with its own risk — but everything
load-bearing in it is carried over below, and the two must not be allowed to
disagree silently. See "Carried over" below.

--------------------------------------------------------------------------
Revisions are append-only. That is the whole design.
--------------------------------------------------------------------------

A venue's history is a tuple of `VenueRecord`s ordered by `effective_from`.
Nothing here ever edits a record: a surface replacement or a roof retrofit is a
NEW record whose `effective_from` is the install date, and the prior record is
CLOSED by `build_revisions` deriving its `effective_to` from the next record's
`effective_from`.

`effective_to` is therefore **not stored**. It is computed, every time, from the
neighbour. That is not a convenience — it is what makes "the adapter overwrote
the record in place" unrepresentable rather than merely discouraged. There is no
field to overwrite.

The failure this defends against is the one the spec names, and it is silent:
an adapter that mutates a record retroactively applies a mid-season surface
change to the whole season, so a Week 2 game is attributed a surface that was
not installed until Week 11. Nothing looks broken. The season simply becomes
internally consistent with a fiction. `revision_on` is the read-time join that
catches it, and `venue/tests/test_revisions.py` is where it is asserted.

--------------------------------------------------------------------------
What this table claims, and from when
--------------------------------------------------------------------------

`TABLE_COMPILED_ON` is the date this table was compiled, and every record's
`effective_from` today. It is deliberately **not** a construction date and not
an epoch sentinel: the table asserts its contents from that date forward and
makes **no claim whatsoever** about any earlier date.

That is the honest shape rather than a limitation to apologise for. Back-dating
these records to a stadium's opening year would assert that today's surface and
roof were true in 1957, which is precisely the retroactive fiction above. A
kickoff before `TABLE_COMPILED_ON` therefore resolves to **no** revision, and
the capture records the game as expected-and-missing with a reason — the same
"refuse rather than guess" answer `resolve_venue_id` gives an unrecognised
stadium name.

The 2026 season begins in September, so every 2026 game is inside the window.

--------------------------------------------------------------------------
Which fields are populated, which are null, and why
--------------------------------------------------------------------------

Populated on every record:

    venue_id, name, city, country, latitude, longitude, timezone, roof_type

    Geo and zones are carried verbatim from `schedule_context/venues.py`,
    which is already reviewed and in production in this repo. `roof_type` is
    structural: it changes only with a retrofit, which is exactly what mints a
    revision.

`home_team_ids` is **derived**, not written: it is inverted from
`_HOME_VENUE_IDS` at import. Two clubs sharing a building therefore cannot
drift apart from the map that put them there, and a neutral-only site gets `[]`
for free.

Populated selectively, null elsewhere:

    altitude_ft         Only `empower-field` (5280) and `azteca` (7280) — the
                        two venues where altitude is a first-order effect on
                        kicking distance and conditioning, and where the
                        published field elevation is unambiguous. Every other
                        venue is null rather than a sea-level-ish guess.

    surface_class       Populated where the surface has been stable through
                        the compile date. NULL on `gillette` and `highmark`,
                        both of which have had a recent surface or building
                        change this table cannot confirm as of
                        TABLE_COMPILED_ON, and NULL on every neutral-site
                        venue, whose pitch is re-laid per event and whose
                        state on a given Sunday is not a property of the
                        building.

Null on every record, and the reason each one is null rather than filled in:

    surface_product     The spec calls this "the genuinely hard part" itself:
                        sources report "turf" or "grass" and the
                        fantasy-relevant difference is between generations of
                        one manufacturer's product. No source consulted here
                        distinguishes them. A confident-looking
                        `field_turf_core` would be an invention.

    surface_installed_on / surface_last_resurfaced_on
                        Undated for the same reason. Note the consequence,
                        because it is the honest tell the spec asks for: since
                        no surface change has a sourced install date, NO venue
                        in this table has a second revision. `venue`'s own
                        `venue_single_revision_venues` gauge reports that
                        count on every pass, so "every venue has exactly one
                        revision" is visible as a fact about the table rather
                        than hidden as an assumption about the world.

    roof_state_policy   A club's game-day operating practice, not a published
                        property of the building. Retractables carry null.

    field_orientation_deg
                        Not published by any source consulted. `weather`'s
                        `crosswind_component_mph` therefore stays null even
                        though this collector now exists — a crosswind resolved
                        against a guessed bearing is worse than an absent one,
                        because it is a number a model will use. The phase doc
                        was corrected to say so rather than left reading "null
                        until `venue` exists".

    seating_capacity, year_built, year_last_renovated
                        "Listed capacity" is a precise published number that
                        moves with every reconfiguration, and renovation years
                        are not sourceable to the standard the rest of this
                        table is held to. Low projection value against real
                        verification cost.

    crowd_noise_profile Derived analytics (`typical_peak_db`,
                        `enclosure_class`, `home_false_start_index`), not
                        reference data. It belongs to whatever measures it.

One classification limitation, stated rather than buried: `sofi` is recorded as
`fixed_dome`. It has a fixed translucent canopy over an open-sided bowl, so it
is rain-proof but not wind-proof, and the spec's three-value enum
(`open`/`fixed_dome`/`retractable`) cannot express that. `fixed_dome` is the
closer of the two available answers and it will mislead a wind model.

--------------------------------------------------------------------------
Carried over from `schedule_context/venues.py`
--------------------------------------------------------------------------

1. **A neutral-site game's venue is NOT the designated home club's stadium.**
   The upstream's `stadium_id` describes the home CLUB's building on those
   rows; only the `stadium` name is correct. Neutral rows therefore resolve by
   stadium NAME, and an unrecognised name resolves to `None` rather than to a
   guess — reading the home club's coordinates for a game in Munich yields
   numbers that pass every schema check and are wrong by four thousand miles.

2. **Zones are IANA, never fixed offsets.** The season crosses the November DST
   transition, `America/Phoenix` does not observe DST while `America/Denver`
   does, Indiana carries its own history in
   `America/Indiana/Indianapolis`, and Las Vegas keeps Pacific rather than
   Mountain time.

3. **Name folding.** The feed spells the same building several ways across
   seasons — accents, punctuation, sponsor renames, and both "Tottenham
   Stadium" and "Tottenham Hotspur Stadium". Every spelling the feed has
   actually used is listed, because an unrecognised one costs a whole row.

4. **Two clubs sharing a building share a `venue_id`**, which is what makes the
   trip between them zero miles.

5. **Neutral aliases reuse the home venue's record** rather than restating
   coordinates, so a correction to a club's location cannot leave its
   neutral-site alias behind.
"""

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date

# The league's home country. `is_international` is "the venue's country differs
# from the league's home country", so this is the thing it differs from.
LEAGUE_COUNTRY = "US"

# The date this table was compiled and the `effective_from` of every record in
# it. See the module docstring: it is not a construction date, and this table
# makes no claim about any earlier date.
TABLE_COMPILED_ON = date(2026, 7, 31)

ROOF_TYPES = frozenset({"open", "fixed_dome", "retractable"})
ROOF_STATE_POLICIES = frozenset(
    {"usually_open", "usually_closed", "game_time_decision"}
)
SURFACE_CLASSES = frozenset({"natural_grass", "hybrid", "synthetic_turf"})

# `venue_game_assignment.home_field_advantage_class`, per the spec.
HFA_NORMAL = "normal"
HFA_SHARED_VENUE = "shared_venue"
HFA_NEUTRAL = "neutral"
HFA_INTERNATIONAL = "international"
HFA_CLASSES = frozenset({HFA_NORMAL, HFA_SHARED_VENUE, HFA_NEUTRAL, HFA_INTERNATIONAL})


@dataclass(frozen=True)
class VenueRecord:
    """One committed assertion about a venue, valid from `effective_from`.

    Deliberately carries no `effective_to`. A record's end is a fact about the
    NEXT record, and storing it here would create the one field an adapter
    could overwrite in place — the failure this collector exists to prevent.
    `build_revisions` derives it.
    """

    venue_id: str
    effective_from: date
    name: str
    city: str
    country: str
    latitude: float
    longitude: float
    timezone: str
    roof_type: str
    altitude_ft: int | None = None
    roof_state_policy: str | None = None
    surface_class: str | None = None
    surface_product: str | None = None
    surface_installed_on: date | None = None
    surface_last_resurfaced_on: date | None = None
    field_orientation_deg: int | None = None
    seating_capacity: int | None = None
    crowd_noise_profile: dict | None = None
    year_built: int | None = None
    year_last_renovated: int | None = None

    def __post_init__(self) -> None:
        # Validated at import rather than at capture time: a typo in a
        # committed table should fail the process that loads it, not produce
        # one schema-invalid row six weeks into a season.
        if self.roof_type not in ROOF_TYPES:
            raise ValueError(f"{self.venue_id}: bad roof_type {self.roof_type!r}")
        if (
            self.roof_state_policy is not None
            and self.roof_state_policy not in ROOF_STATE_POLICIES
        ):
            raise ValueError(
                f"{self.venue_id}: bad roof_state_policy {self.roof_state_policy!r}"
            )
        if self.surface_class is not None and self.surface_class not in SURFACE_CLASSES:
            raise ValueError(
                f"{self.venue_id}: bad surface_class {self.surface_class!r}"
            )


@dataclass(frozen=True)
class VenueRevision:
    """A `VenueRecord` with its validity window closed and tenants attached.

    `effective_to` is exclusive: the window is `[effective_from, effective_to)`,
    so a record replaced on the day of an install does not overlap its
    successor by one day. `None` means "still current".
    """

    record: VenueRecord
    effective_to: date | None
    home_team_ids: tuple[str, ...] = ()

    @property
    def venue_id(self) -> str:
        return self.record.venue_id

    @property
    def effective_from(self) -> date:
        return self.record.effective_from

    def contains(self, day: date) -> bool:
        """Whether this revision was the record true on `day`.

        Half-open on purpose. A closed-closed window would make the install
        date belong to both the old record and the new one, and a join that
        must return exactly one revision would then return two on precisely
        the day the change happened.
        """
        if day < self.effective_from:
            return False
        return self.effective_to is None or day < self.effective_to


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def build_revisions(records: tuple[VenueRecord, ...]) -> tuple[VenueRevision, ...]:
    """Close each record at the next one's `effective_from`. Never mutates.

    Rejects an unordered or duplicated `effective_from`, because both break the
    property the read-time join relies on: that exactly one revision contains
    any given day inside the table's claim window. Two records sharing a date
    would make `revision_on` ambiguous, and an out-of-order pair would produce
    a window with a negative length that `contains` would answer `False` for
    every day — a venue that silently exists in no revision at all.
    """
    if not records:
        return ()

    ordered = tuple(records)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if earlier.effective_from >= later.effective_from:
            raise ValueError(
                f"{earlier.venue_id}: revisions must be strictly ordered by "
                f"effective_from; got {earlier.effective_from} then "
                f"{later.effective_from}"
            )

    venue_id = ordered[0].venue_id
    if any(record.venue_id != venue_id for record in ordered):
        raise ValueError(f"{venue_id}: revision list mixes venue_ids")

    tenants = HOME_TEAM_IDS.get(venue_id, ())
    return tuple(
        VenueRevision(
            record=record,
            effective_to=(
                ordered[index + 1].effective_from if index + 1 < len(ordered) else None
            ),
            home_team_ids=tenants,
        )
        for index, record in enumerate(ordered)
    )


# ── the clubs' home buildings ────────────────────────────────────────────────
#
# Club -> venue_id. Two pairs share a building (the two Los Angeles clubs and
# the two New York clubs), which is why the venue id is data rather than the
# club's own name. `home_team_ids` on every revision is inverted from this map
# rather than written twice.
_HOME_VENUE_IDS: dict[str, str] = {
    "ARI": "state-farm",
    "ATL": "mercedes-benz",
    "BAL": "mt-bank",
    "BUF": "highmark",
    "CAR": "bank-of-america",
    "CHI": "soldier-field",
    "CIN": "paycor",
    "CLE": "huntington-bank-field",
    "DAL": "att",
    "DEN": "empower-field",
    "DET": "ford-field",
    "GB": "lambeau",
    "HOU": "nrg",
    "IND": "lucas-oil",
    "JAX": "everbank",
    "KC": "arrowhead",
    "LA": "sofi",
    "LAC": "sofi",
    "LV": "allegiant",
    "MIA": "hard-rock",
    "MIN": "us-bank",
    "NE": "gillette",
    "NO": "caesars-superdome",
    "NYG": "metlife",
    "NYJ": "metlife",
    "PHI": "lincoln-financial",
    "PIT": "acrisure",
    "SEA": "lumen",
    "SF": "levis",
    "TB": "raymond-james",
    "TEN": "nissan",
    "WAS": "northwest",
}


def _invert_tenants(home_venue_ids: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """venue_id -> the clubs that call it home, in club order.

    Derived rather than written: a shared building must not be able to list one
    tenant here and two in the club map.
    """
    tenants: dict[str, list[str]] = {}
    for club, venue_id in home_venue_ids.items():
        tenants.setdefault(venue_id, []).append(club)
    return {venue_id: tuple(clubs) for venue_id, clubs in tenants.items()}


HOME_TEAM_IDS: dict[str, tuple[str, ...]] = _invert_tenants(_HOME_VENUE_IDS)


def _club_venue(
    venue_id: str,
    name: str,
    city: str,
    latitude: float,
    longitude: float,
    timezone: str,
    roof_type: str,
    *,
    surface_class: str | None = None,
    altitude_ft: int | None = None,
) -> VenueRecord:
    """One league building, as of `TABLE_COMPILED_ON`.

    A helper rather than 30 fully-spelled constructors so the fields that are
    deliberately null stay null by default: a reviewer reading this table sees
    exactly the facts that were sourced, and adding a guess requires typing it.
    """
    return VenueRecord(
        venue_id=venue_id,
        effective_from=TABLE_COMPILED_ON,
        name=name,
        city=city,
        country=LEAGUE_COUNTRY,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        roof_type=roof_type,
        surface_class=surface_class,
        altitude_ft=altitude_ft,
    )


# Coordinates and zones are carried verbatim from
# `schedule_context/venues.py`. `roof_type` and `surface_class` are new here;
# see the module docstring for exactly which of them were sourced and which
# were deliberately left null.
_CLUB_RECORDS: tuple[VenueRecord, ...] = (
    _club_venue(
        "state-farm",
        "State Farm Stadium",
        "Glendale",
        33.5276,
        -112.2626,
        # Arizona does not observe DST, so its offset relative to the rest of
        # the Mountain zone changes mid-season. America/Phoenix, not
        # America/Denver.
        "America/Phoenix",
        "retractable",
        surface_class="natural_grass",
    ),
    _club_venue(
        "mercedes-benz",
        "Mercedes-Benz Stadium",
        "Atlanta",
        33.7554,
        -84.4008,
        "America/New_York",
        "retractable",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "mt-bank",
        "M&T Bank Stadium",
        "Baltimore",
        39.2780,
        -76.6227,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "highmark",
        "Highmark Stadium",
        "Orchard Park",
        42.7738,
        -78.7870,
        "America/New_York",
        "open",
        # surface_class deliberately null: this building has had a recent
        # change this table cannot confirm as of TABLE_COMPILED_ON.
    ),
    _club_venue(
        "bank-of-america",
        "Bank of America Stadium",
        "Charlotte",
        35.2258,
        -80.8528,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "soldier-field",
        "Soldier Field",
        "Chicago",
        41.8623,
        -87.6167,
        "America/Chicago",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "paycor",
        "Paycor Stadium",
        "Cincinnati",
        39.0955,
        -84.5161,
        "America/New_York",
        "open",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "huntington-bank-field",
        "Huntington Bank Field",
        "Cleveland",
        41.5061,
        -81.6995,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "att",
        "AT&T Stadium",
        "Arlington",
        32.7473,
        -97.0945,
        "America/Chicago",
        "retractable",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "empower-field",
        "Empower Field at Mile High",
        "Denver",
        39.7439,
        -105.0201,
        "America/Denver",
        "open",
        surface_class="natural_grass",
        # The one altitude in the league that is a first-order effect on
        # kicking distance, and the one whose published field elevation is
        # unambiguous.
        altitude_ft=5280,
    ),
    _club_venue(
        "ford-field",
        "Ford Field",
        "Detroit",
        42.3400,
        -83.0456,
        "America/Detroit",
        "fixed_dome",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "lambeau",
        "Lambeau Field",
        "Green Bay",
        44.5013,
        -88.0622,
        "America/Chicago",
        "open",
        surface_class="hybrid",
    ),
    _club_venue(
        "nrg",
        "NRG Stadium",
        "Houston",
        29.6847,
        -95.4107,
        "America/Chicago",
        "retractable",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "lucas-oil",
        "Lucas Oil Stadium",
        "Indianapolis",
        39.7601,
        -86.1639,
        # America/Indiana/Indianapolis, not America/New_York: Indiana did not
        # observe DST until 2006 and the zone carries that history.
        "America/Indiana/Indianapolis",
        "retractable",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "everbank",
        "EverBank Stadium",
        "Jacksonville",
        30.3239,
        -81.6373,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "arrowhead",
        "GEHA Field at Arrowhead Stadium",
        "Kansas City",
        39.0489,
        -94.4839,
        "America/Chicago",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "sofi",
        "SoFi Stadium",
        "Inglewood",
        33.9535,
        -118.3392,
        # A fixed translucent canopy over an open-sided bowl: rain-proof, not
        # wind-proof. The spec's three-value enum cannot express that, and
        # `fixed_dome` is the closer of the two available answers. See the
        # module docstring — this WILL mislead a wind model.
        "America/Los_Angeles",
        "fixed_dome",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "allegiant",
        "Allegiant Stadium",
        # Las Vegas keeps Pacific time, not Mountain.
        "Paradise",
        36.0909,
        -115.1833,
        "America/Los_Angeles",
        "fixed_dome",
        surface_class="natural_grass",
    ),
    _club_venue(
        "hard-rock",
        "Hard Rock Stadium",
        "Miami Gardens",
        25.9580,
        -80.2389,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "us-bank",
        "U.S. Bank Stadium",
        "Minneapolis",
        44.9736,
        -93.2575,
        "America/Chicago",
        "fixed_dome",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "gillette",
        "Gillette Stadium",
        "Foxborough",
        42.0909,
        -71.2643,
        "America/New_York",
        "open",
        # surface_class deliberately null: a recent change this table cannot
        # confirm as of TABLE_COMPILED_ON.
    ),
    _club_venue(
        "caesars-superdome",
        "Caesars Superdome",
        "New Orleans",
        29.9511,
        -90.0812,
        "America/Chicago",
        "fixed_dome",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "metlife",
        "MetLife Stadium",
        "East Rutherford",
        40.8135,
        -74.0745,
        "America/New_York",
        "open",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "lincoln-financial",
        "Lincoln Financial Field",
        "Philadelphia",
        39.9008,
        -75.1675,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "acrisure",
        "Acrisure Stadium",
        "Pittsburgh",
        40.4468,
        -80.0158,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "lumen",
        "Lumen Field",
        "Seattle",
        47.5952,
        -122.3316,
        "America/Los_Angeles",
        "open",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "levis",
        "Levi's Stadium",
        "Santa Clara",
        37.4033,
        -121.9694,
        "America/Los_Angeles",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "raymond-james",
        "Raymond James Stadium",
        "Tampa",
        27.9759,
        -82.5033,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
    _club_venue(
        "nissan",
        "Nissan Stadium",
        "Nashville",
        36.1665,
        -86.7713,
        "America/Chicago",
        "open",
        surface_class="synthetic_turf",
    ),
    _club_venue(
        "northwest",
        "Northwest Stadium",
        "Landover",
        38.9077,
        -76.8645,
        "America/New_York",
        "open",
        surface_class="natural_grass",
    ),
)


def _neutral_venue(
    venue_id: str,
    name: str,
    city: str,
    country: str,
    latitude: float,
    longitude: float,
    timezone: str,
    roof_type: str,
) -> VenueRecord:
    """A venue outside the league's own buildings.

    `surface_class` takes no argument at all here, rather than defaulting to
    null: these pitches are re-laid per event, so the surface on a given Sunday
    is a property of the event and not of the building. Offering the parameter
    would invite somebody to fill it in.
    """
    return VenueRecord(
        venue_id=venue_id,
        effective_from=TABLE_COMPILED_ON,
        name=name,
        city=city,
        country=country,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        roof_type=roof_type,
    )


_NEUTRAL_RECORDS: tuple[VenueRecord, ...] = (
    _neutral_venue(
        "wembley",
        "Wembley Stadium",
        "London",
        "GB",
        51.5560,
        -0.2795,
        "Europe/London",
        "retractable",
    ),
    _neutral_venue(
        "twickenham",
        "Twickenham Stadium",
        "London",
        "GB",
        51.4560,
        -0.3415,
        "Europe/London",
        "open",
    ),
    _neutral_venue(
        "tottenham",
        "Tottenham Hotspur Stadium",
        "London",
        "GB",
        51.6043,
        -0.0665,
        "Europe/London",
        "open",
    ),
    _neutral_venue(
        "allianz",
        "Allianz Arena",
        "Munich",
        "DE",
        48.2188,
        11.6247,
        "Europe/Berlin",
        "open",
    ),
    _neutral_venue(
        "frankfurt",
        "Deutsche Bank Park",
        "Frankfurt",
        "DE",
        50.0685,
        8.6455,
        "Europe/Berlin",
        "retractable",
    ),
    _neutral_venue(
        "azteca",
        "Estadio Azteca",
        "Mexico City",
        "MX",
        19.3029,
        -99.1505,
        "America/Mexico_City",
        "open",
    ),
    _neutral_venue(
        "neo-quimica",
        "Neo Quimica Arena",
        "Sao Paulo",
        "BR",
        -23.5453,
        -46.4742,
        "America/Sao_Paulo",
        "open",
    ),
    _neutral_venue(
        "maracana",
        "Maracana Stadium",
        "Rio de Janeiro",
        "BR",
        -22.9121,
        -43.2302,
        "America/Sao_Paulo",
        "open",
    ),
    _neutral_venue(
        "croke-park",
        "Croke Park",
        "Dublin",
        "IE",
        53.3607,
        -6.2512,
        "Europe/Dublin",
        "open",
    ),
    _neutral_venue(
        "bernabeu",
        "Santiago Bernabeu",
        "Madrid",
        "ES",
        40.4531,
        -3.6883,
        "Europe/Madrid",
        "retractable",
    ),
    _neutral_venue(
        "stade-de-france",
        "Stade de France",
        "Saint-Denis",
        "FR",
        48.9245,
        2.3601,
        "Europe/Paris",
        "open",
    ),
    _neutral_venue(
        "mcg",
        "Melbourne Cricket Ground",
        "Melbourne",
        "AU",
        -37.8200,
        144.9834,
        "Australia/Melbourne",
        "open",
    ),
    _neutral_venue(
        "rogers-centre",
        "Rogers Centre",
        "Toronto",
        "CA",
        43.6414,
        -79.3894,
        "America/Toronto",
        "retractable",
    ),
)

# The altitude of Mexico City is the second and last one this table asserts;
# applied here rather than in `_neutral_venue` so the helper keeps offering no
# way to add a guessed one.
_NEUTRAL_RECORDS = tuple(
    replace(record, altitude_ft=7280) if record.venue_id == "azteca" else record
    for record in _NEUTRAL_RECORDS
)


def _group(records: tuple[VenueRecord, ...]) -> dict[str, tuple[VenueRecord, ...]]:
    grouped: dict[str, list[VenueRecord]] = {}
    for record in records:
        grouped.setdefault(record.venue_id, []).append(record)
    return {
        venue_id: tuple(sorted(items, key=lambda r: r.effective_from))
        for venue_id, items in grouped.items()
    }


VENUE_RECORDS: dict[str, tuple[VenueRecord, ...]] = _group(
    (*_CLUB_RECORDS, *_NEUTRAL_RECORDS)
)

# The table the collector actually reads. Built once at import: the source
# records are immutable and so is this, which is the other half of "the adapter
# never mutates a record" — there is nothing here to mutate.
REVISIONS: dict[str, tuple[VenueRevision, ...]] = {
    venue_id: build_revisions(records) for venue_id, records in VENUE_RECORDS.items()
}


# ── resolving a game's venue ─────────────────────────────────────────────────

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

# Accented characters the upstream spells inconsistently across seasons
# ("Estadio Azteca" / "Estádio Azteca", "Neo Química Arena"). Folded before the
# alphanumeric squeeze so both spellings land on one key.
_FOLD = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüçñ", "aaaaaeeeeiiiiooooouuuucn")


def normalize_stadium(name: str) -> str:
    """Fold a stadium name to its lookup key.

    Upstream spelling varies across seasons in punctuation, capitalisation and
    accents, none of which change which building it is.
    """
    return _NON_ALPHANUMERIC.sub("", name.strip().lower().translate(_FOLD))


# Every spelling the feed has actually used for a non-league venue. Keyed by
# NAME because `stadium_id` on a neutral row is unreliable: the feed has spelled
# Wembley's as both LON00 and JAX00 and Tottenham's as WAS00, because on some
# rows it carries the DESIGNATED HOME CLUB's building instead of the venue.
_NEUTRAL_NAMES: dict[str, str] = {
    "Wembley Stadium": "wembley",
    "Twickenham Stadium": "twickenham",
    "Tottenham Stadium": "tottenham",
    "Tottenham Hotspur Stadium": "tottenham",
    "Allianz Arena": "allianz",
    "FC Bayern Munich Stadium": "allianz",
    "Deutsche Bank Park": "frankfurt",
    "Estadio Azteca": "azteca",
    "Azteca Stadium": "azteca",
    "Estadio Banorte": "azteca",
    "Neo Química Arena": "neo-quimica",
    "Arena Corinthians": "neo-quimica",
    "Maracana Stadium": "maracana",
    "Croke Park": "croke-park",
    "Santiago Bernabéu": "bernabeu",
    "Bernabeu": "bernabeu",
    "Stade de France": "stade-de-france",
    "Melbourne Cricket Ground": "mcg",
    "Rogers Centre": "rogers-centre",
}

# League buildings that also host neutral-site games — a Super Bowl, a hurricane
# relocation, an international game moved home. Several are FORMER names of a
# building the club still plays in, which is exactly the drift a name lookup has
# to survive. They map to the club's own venue_id rather than restating any
# record, so a correction to a building cannot leave its alias behind.
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

NEUTRAL_VENUE_IDS: dict[str, str] = {
    **{normalize_stadium(name): venue_id for name, venue_id in _NEUTRAL_NAMES.items()},
    **{
        normalize_stadium(name): _HOME_VENUE_IDS[club]
        for name, club in _NEUTRAL_ALIASES.items()
    },
}


def resolve_venue_id(
    *, home_team: str, stadium_name: str, is_neutral_site: bool
) -> str | None:
    """Where a game is actually played, or `None` if that cannot be determined.

    `None` rather than a fallback, on purpose. A guessed venue produces
    coordinates, timezone and roof that are plausible, schema-valid and wrong —
    which is precisely the failure the coverage block exists to make visible.
    The caller records the game as expected-and-missing with a reason instead.

    Carried over from `schedule_context/venues.py`: a neutral-site game resolves
    by stadium NAME, never by the designated home club, because the feed's
    `stadium_id` on those rows describes the home club's own building.
    """
    if is_neutral_site:
        return NEUTRAL_VENUE_IDS.get(normalize_stadium(stadium_name))
    return _HOME_VENUE_IDS.get(home_team.strip().upper())


def revisions_containing(venue_id: str, day: date) -> tuple[VenueRevision, ...]:
    """Every revision whose window contains `day`. Should always be 0 or 1.

    Returns a tuple rather than an Optional so "exactly one" can be **checked**
    instead of assumed. Non-overlap is a property of how `build_revisions`
    closes windows — strictly ordered records, half-open intervals — and a
    property nothing verifies is not a property. Widening `contains` to a
    closed-closed interval, for instance, makes an install date belong to two
    revisions at once, and a caller written against an Optional would silently
    take whichever came first.
    """
    return tuple(
        revision for revision in REVISIONS.get(venue_id, ()) if revision.contains(day)
    )


def revision_on(venue_id: str, day: date) -> VenueRevision | None:
    """The revision whose `[effective_from, effective_to)` window contains `day`.

    This is the read-time join the spec names. `None` means the table makes no
    claim about that venue on that day — a kickoff before `TABLE_COMPILED_ON`,
    or a venue this table does not carry. It is never a fallback to "the most
    recent revision", because that fallback IS the failure mode: it attributes a
    surface installed in November to a game played in September.

    `None` is also returned when more than one revision matches, which cannot
    happen with a well-formed table and must not be papered over by picking
    one. The caller records the venue as missing with a reason either way.
    """
    matches = revisions_containing(venue_id, day)
    return matches[0] if len(matches) == 1 else None


def revisions_for(venue_id: str) -> tuple[VenueRevision, ...]:
    """A venue's full ordered revision history. Empty for an unknown venue."""
    return REVISIONS.get(venue_id, ())


def home_field_advantage_class(
    revision: VenueRevision,
    *,
    designated_home_team_id: str,
    is_neutral_site: bool,
) -> str:
    """Classify what "home" is worth for one game.

    Ordering is the substance. `international` outranks `neutral` because every
    international game is also neutral and the longer answer is the more useful
    one; `neutral` outranks `shared_venue` because a Super Bowl at a club's own
    building confers no home advantage on either participant even though the
    building has tenants.
    """
    if revision.record.country != LEAGUE_COUNTRY:
        return HFA_INTERNATIONAL
    if is_neutral_site:
        return HFA_NEUTRAL
    if (
        len(revision.home_team_ids) > 1
        and designated_home_team_id in revision.home_team_ids
    ):
        return HFA_SHARED_VENUE
    return HFA_NORMAL


# The fields that make up a revision's CONTENT — everything except the
# identity (`venue_id`) and the derived window (`effective_from`/`_to`). This
# is what `content_hash` digests, and it is what "the venue's record changed"
# means. Named here rather than inferred from the dataclass so adding a field
# to `VenueRecord` without deciding whether it belongs in the hash is a visible
# omission rather than a silent inclusion.
STATIC_CONTENT_FIELDS: tuple[str, ...] = (
    "name",
    "city",
    "country",
    "latitude",
    "longitude",
    "timezone",
    "altitude_ft",
    "roof_type",
    "roof_state_policy",
    "surface_class",
    "surface_product",
    "surface_installed_on",
    "surface_last_resurfaced_on",
    "field_orientation_deg",
    "seating_capacity",
    "crowd_noise_profile",
    "year_built",
    "year_last_renovated",
)


def revision_to_row(revision: VenueRevision) -> dict:
    """One revision as a `venue_static` signal row.

    Dates are ISO-8601 strings and `effective_to` is `None` for the current
    revision — the shape `contracts/signal-envelope/collectors/venue.json`
    validates, and the shape the read-time join needs.
    """
    record = revision.record
    return {
        "venue_id": record.venue_id,
        "effective_from": _iso(record.effective_from),
        "effective_to": _iso(revision.effective_to),
        "name": record.name,
        "city": record.city,
        "country": record.country,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "timezone": record.timezone,
        "altitude_ft": record.altitude_ft,
        "roof_type": record.roof_type,
        "roof_state_policy": record.roof_state_policy,
        "surface_class": record.surface_class,
        "surface_product": record.surface_product,
        "surface_installed_on": _iso(record.surface_installed_on),
        "surface_last_resurfaced_on": _iso(record.surface_last_resurfaced_on),
        "field_orientation_deg": record.field_orientation_deg,
        "seating_capacity": record.seating_capacity,
        "crowd_noise_profile": record.crowd_noise_profile,
        "year_built": record.year_built,
        "year_last_renovated": record.year_last_renovated,
        "home_team_ids": list(revision.home_team_ids),
        "content_hash": content_hash(revision),
    }


def content_hash(revision: VenueRevision) -> str:
    """sha256 over a revision's CONTENT — the thing that decides a new snapshot.

    Two exclusions, both deliberate:

    **`venue_id`**, because the hash is already scoped to one venue and
    including it would make every hash unique by construction, which is the
    same as having no hash.

    **`effective_from` / `effective_to`**, because a window is not content. Two
    revisions carrying identical facts under different dates must hash the
    same — that is exactly what "this venue's record did not change" means, and
    it is what lets a `static reference` collector re-read daily without
    appending an identical snapshot every day. The revision LIST's digest (see
    `venue.capture`) does cover the windows, so appending or closing a revision
    still changes the pass.

    `sort_keys` and a separator-tight dump, so the digest depends on the values
    and not on dict insertion order or json's default spacing.
    """
    row = {name: getattr(revision.record, name) for name in STATIC_CONTENT_FIELDS}
    row["surface_installed_on"] = _iso(revision.record.surface_installed_on)
    row["surface_last_resurfaced_on"] = _iso(revision.record.surface_last_resurfaced_on)
    row["home_team_ids"] = list(revision.home_team_ids)
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

"""What a canonical Foundry player record *is*.

Deliberately split from `resolution.py`. This module owns the *shape* of a
canonical record — id minting, the suffix split, the match key, position
grouping, and the `external_ids` crosswalk block. `resolution.py` owns how a
raw string becomes one of these records. The two change for different
reasons: adding an upstream crosswalk source is a change here; retuning the
attribute weights is a change there. Weather carries five domain modules for
the same reason, so two is in-family rather than a new pattern.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

PLAYER_ID_PREFIX = "fdy-"

# Recognized generational suffixes, held separately so they never pollute the
# match key: "Odell Beckham Jr" and "Odell Beckham" must produce one key.
_SUFFIX_CANONICAL = {
    "jr": "Jr",
    "jr.": "Jr",
    "sr": "Sr",
    "sr.": "Sr",
    "ii": "II",
    "iii": "III",
    "iv": "IV",
    "v": "V",
}

# Hyphens become spaces rather than vanishing: a book writing "Amon Ra St
# Brown" and a roster feed writing "Amon-Ra St. Brown" must normalize to the
# same key, which deleting the hyphen outright would not achieve.
_HYPHENS = re.compile(r"[-‐-―]")
_PUNCT = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")

# `position_group` per the Phase 8 spec. Defensive codes are carried for
# disambiguation only — a WR and a safety who share a name separate here.
_POSITION_GROUPS: dict[str, str] = {
    "QB": "offense_skill",
    "RB": "offense_skill",
    "FB": "offense_skill",
    "WR": "offense_skill",
    "TE": "offense_skill",
    "C": "offense_line",
    "G": "offense_line",
    "OG": "offense_line",
    "OL": "offense_line",
    "OT": "offense_line",
    "T": "offense_line",
    "DE": "defense",
    "DT": "defense",
    "DL": "defense",
    "NT": "defense",
    "LB": "defense",
    "ILB": "defense",
    "OLB": "defense",
    "MLB": "defense",
    "CB": "defense",
    "DB": "defense",
    "S": "defense",
    "FS": "defense",
    "SS": "defense",
    "K": "special_teams",
    "P": "special_teams",
    "LS": "special_teams",
    "DST": "special_teams",
    "DEF": "special_teams",
}

KNOWN_POSITIONS = frozenset(_POSITION_GROUPS)

# Upstream keys carrying a *published* crosswalk id, mapped to the source name
# used in `external_ids`. Tier 1 linking adopts these rather than matching on
# anything: an upstream carrying one of them joins exactly, with no scoring.
#
# This mapping is also the document-level schema assertion in
# `adapters/sleeper.py`: a rename of `gsis_id` upstream would otherwise
# silently drop every Tier-1 link while the capture still looked healthy.
CROSSWALK_KEYS: dict[str, str] = {
    "gsis_id": "gsis",
    "espn_id": "espn",
    "yahoo_id": "yahoo",
    "rotowire_id": "rotowire",
    "sportradar_id": "sportradar",
    "fantasy_data_id": "fantasy_data",
}

CROSSWALK_SOURCES = frozenset(CROSSWALK_KEYS.values())

# The upstream whose own record key anchors the canonical id (below).
ANCHOR_SOURCE = "sleeper"

_ROSTER_STATUS = {
    "active": "active",
    "inactive": "inactive",
    "injured reserve": "ir",
    "ir": "ir",
    "physically unable to perform": "pup",
    "pup": "pup",
    "practice squad": "practice_squad",
    "non football injury": "inactive",
    "suspended": "suspended",
    "retired": "retired",
}


def split_suffix(name: str) -> tuple[str, str | None]:
    """Split a trailing generational suffix off a display name.

    Returns `(base_name, canonical_suffix_or_None)`. The suffix is held
    separately on the record so `normalized_key` never sees it — two feeds
    that disagree only on whether they print "Jr" must still match.
    """
    parts = (name or "").split()
    if len(parts) >= 2:
        canonical = _SUFFIX_CANONICAL.get(parts[-1].lower())
        if canonical is not None:
            return " ".join(parts[:-1]), canonical
    return " ".join(parts), None


def normalized_key(name: str) -> str:
    """Lowercased, diacritics-folded, punctuation-stripped, suffix-removed.

    Not unique on its own — that is the whole reason Tier 3 scores several
    attributes rather than keying on this one.
    """
    base, _ = split_suffix(name or "")
    folded = unicodedata.normalize("NFKD", base)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _HYPHENS.sub(" ", folded.lower())
    folded = _PUNCT.sub("", folded)
    return _WS.sub(" ", folded).strip()


def position_group(position: str | None) -> str | None:
    return _POSITION_GROUPS.get((position or "").upper()) or None


def roster_status(status: str | None, team: str | None) -> str:
    """Map an upstream status string onto the contracted enum.

    An unknown or absent status with no team is a free agent; with a team it
    is an active roster spot. Guessing anything finer would fabricate detail
    the upstream did not supply.
    """
    mapped = _ROSTER_STATUS.get((status or "").strip().lower())
    if mapped is not None:
        return mapped
    return "active" if team else "free_agent"


def mint_player_id(anchor_source: str, anchor_id: str) -> str:
    """Derive the canonical `fdy-` id from the upstream's own stable key.

    Anchored on the upstream record key rather than on the highest-priority
    crosswalk id present. That choice matters: anchoring on "best available
    crosswalk id" re-mints the id the day a player finally gains a `gsis_id`,
    which breaks the contract that a `player_id` is stable for a career and
    never reissued. The upstream key is present on every record by
    construction, so this is derivable, deterministic, and stable across a
    full rebuild.
    """
    digest = hashlib.sha1(f"{anchor_source}:{anchor_id}".encode()).hexdigest()
    return f"{PLAYER_ID_PREFIX}{digest[:12]}"


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class IdentityRecord:
    """One canonical player, serialized as a `player_identity_crosswalk` row."""

    player_id: str
    full_name: str
    first_name: str
    last_name: str
    name_suffix: str | None
    normalized_key: str
    position: str
    position_group: str
    jersey_number: int | None
    jersey_as_of: datetime
    team: str | None
    team_as_of: datetime
    roster_status: str
    birth_date: str | None
    entry_year: int | None
    external_ids: dict[str, dict]
    last_verified_at: datetime
    aliases: list[dict] = field(default_factory=list)
    superseded_by: str | None = None

    def to_signal(self) -> dict:
        return {
            "player_id": self.player_id,
            "full_name": self.full_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "name_suffix": self.name_suffix,
            "normalized_key": self.normalized_key,
            "aliases": list(self.aliases),
            "position": self.position,
            "position_group": self.position_group,
            "jersey_number": self.jersey_number,
            "jersey_as_of": _rfc3339(self.jersey_as_of),
            "team": self.team,
            "team_as_of": _rfc3339(self.team_as_of),
            "roster_status": self.roster_status,
            "birth_date": self.birth_date,
            "entry_year": self.entry_year,
            "external_ids": dict(self.external_ids),
            "superseded_by": self.superseded_by,
            "last_verified_at": _rfc3339(self.last_verified_at),
        }


def crosswalk_external_ids(record: dict, now: datetime) -> dict[str, dict]:
    """Build the Tier-1 `external_ids` block from a published crosswalk row.

    `link_method` is `crosswalk` and `match_score`/`match_margin` are null:
    nothing was matched. Recording a score of 1.0 here would make an adopted
    link indistinguishable from a perfect Tier-3 one in the miss analysis.
    """
    linked_at = _rfc3339(now)
    out: dict[str, dict] = {}
    for key, source in CROSSWALK_KEYS.items():
        value = record.get(key)
        if value in (None, ""):
            continue
        out[source] = {
            "id": str(value),
            "linked_at": linked_at,
            "link_method": "crosswalk",
            "match_score": None,
            "match_margin": None,
        }
    return out

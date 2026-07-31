"""What "complete" means for depth-chart, declared before any fetch happens.

The Phase 8 spec is explicit that this collector's `coverage.expected` is *"one
ordering per `(team, position)` pair across all 32 teams for the configured
position set — coverage counts position groups, not players, so a team
publishing a chart with a position group omitted registers as missing."*

That number is config, not observation, so it lives in its own module the way
roster-scope's `rules.py` does. Both `capture.py` (which floors coverage against
it) and `adapters/upstream.py` (which drops rows outside it as they stream past)
read from here, and neither computes it from a document.

The vendor-label maps below deliberately duplicate roster-scope's rather than
importing them: services do not import each other, only `collector-core`. Three
collectors now carry a team/position canonicalisation table, which is a real
signal that it belongs in the shared library — flagged as a follow-up rather
than fixed here, because moving it is a `collector-core` change that has to land
once for the whole fleet, not six times.
"""

# All 32, always. The universe is defined by config, not by whichever teams an
# upstream happened to return today — that is the entire point of the floor.
TEAMS: tuple[str, ...] = (
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
)  # fmt: skip

# The configured position set: the groups this collector publishes an ordering
# for. Offensive skill positions plus the kicker — the positions a fantasy
# projection is actually taken over. The chart carries the full two-deep for the
# offensive line, the whole defense and the rest of special teams; ordering
# those would be ~70% more rows that nothing downstream reads.
#
# Deliberately NOT narrowed to roster-scope's membership list: the spec's
# `scope_aware: no` reasoning is that "ranking only watchlist players hides the
# backup one move away from entering it". Every charted player at these
# positions is published, however deep.
POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K")

# Relocations and the two Los Angeles clubs. An unknown label is dropped rather
# than guessed at, and its groups then read as missing — which is the correct
# accounting for "we have no chart for this team".
TEAM_ALIASES: dict[str, str] = {
    "JAC": "JAX",
    "LA": "LAR",
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
    "WSH": "WAS",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
}

# Vendor position labels collapsed onto `POSITIONS`. The spec's adapter notes
# make this mandatory: "It must normalize vendor-specific position labels
# (`WR1/WR2/WR3` versus `LWR/RWR/SWR`) onto the platform's position set before
# ranking, since some upstreams order by field side rather than by quality."
POSITION_ALIASES: dict[str, str] = {
    "QB": "QB",
    "RB": "RB", "HB": "RB", "TB": "RB", "FB": "RB",
    "WR": "WR", "SE": "WR", "FL": "WR", "SLOT": "WR",
    "LWR": "WR", "RWR": "WR", "SWR": "WR", "X": "WR", "Z": "WR",
    "TE": "TE", "Y": "TE",
    "K": "K", "PK": "K",
}  # fmt: skip


def canonical_team(raw: str) -> str | None:
    """Canonical abbreviation, or None when the label names no known team."""
    label = (raw or "").strip().upper()
    label = TEAM_ALIASES.get(label, label)
    return label if label in TEAMS else None


def canonical_position(raw: str) -> str | None:
    """Canonical position group, or None for a label outside the configured set.

    Returning None rather than passing the raw label through is what keeps a
    linebacker out of a `WR` ordering, and what makes an unrecognised label a
    dropped row rather than a 33rd position group nobody expected.
    """
    return POSITION_ALIASES.get((raw or "").strip().upper())


def group_key(team: str, position: str) -> str:
    """`<team>:<position>` — the coverage accounting unit.

    The group, not the player: a team that publishes a chart with its whole TE
    room omitted must register one missing group, and a chart listing six
    receivers instead of four must not register as *more* coverage than one
    listing four.
    """
    return f"{team}:{position}"


def expected_groups() -> tuple[str, ...]:
    """Every `(team, position)` ordering the config demands.

    32 teams x 5 positions = **160**. Built from this file alone, before any
    upstream is contacted, so a truncated feed carrying three teams reports
    `expected: 160, present: 15` rather than `expected: 15, present: 15`.
    """
    return tuple(
        group_key(team, position) for team in TEAMS for position in POSITIONS
    )

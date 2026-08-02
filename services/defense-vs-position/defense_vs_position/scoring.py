"""Fantasy scoring, and the counting stats it is computed from. Pure, no I/O.

Split out of the adapter on purpose: every number this collector publishes is
a fold over `StatLine`/`DstLine`, so the folding is testable without a wire
format and the wire format is testable without arithmetic.

**Both bases come from one accumulator, structurally.** The spec requires
`fantasy_points_allowed_per_game` and `fantasy_points_allowed_per_opportunity`
to derive from the same underlying play set, and the named failure mode is
what happens when they do not agree. Here they cannot diverge by construction:
`games` and `opportunities` are two fields of the same `StatLine`, incremented
by the same pass over the same plays, and the two rates are the same numerator
over two of its denominators. A second, independently filtered fetch is the
shape that makes them different statistics; there is no second fetch.

**Scoring weights are the widely published defaults, and the ones that are a
league setting are named rather than assumed.** Missed field goals carry no
penalty here (many leagues charge -1, and just as many do not), and the
`points_allowed` tiers are the standard ESPN/Yahoo ladder. A consumer that
needs a different ruleset needs a different `scoring_format`, which is exactly
what that dimension is for -- it is not a place to bury a house rule.
"""

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# The dimensions this collector publishes
# --------------------------------------------------------------------------

# The spec's `position` enum, in full.
POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DST")

# The positions whose allowance is attributed through an individual player,
# and therefore through `player-identity`. `DST` is not one: there is no
# player to resolve, because the unit IS the team. See `capture.py`.
PLAYER_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K")

# **The whole `alignment` enum this collector can source.** The spec's enum is
# `slot | perimeter | receiving_back | early_down_back | inline_te |
# detached_te | all`, and `all` is a legal member of it, so this is a
# narrowing *within* the spec's vocabulary rather than a departure from it.
#
# Only `all` is sourceable. `alignment` means where a player lined up on the
# snap in question, and no nflverse feed carries a per-snap alignment column:
# `pbp_participation`'s `offense_positions` is the ROSTER-LISTED position for
# each of the eleven players on the field, not their position on the field,
# and NGS receiving publishes nothing finer. The spec anticipates exactly this
# and names the wrong answer outright -- "without per-snap alignment the
# slot/perimeter split degrades into a season-long player label applied
# retroactively to every snap, which is wrong for any receiver who moves."
#
# Unlocking the sub-splits needs an upstream with a per-snap alignment or
# pre-snap-position column (a charting provider such as PFF or SIS, or an NGS
# tracking feed exposing receiver x/y at the snap). Until one is wired, this
# tuple is one element long and every row says `all`.
ALIGNMENTS: tuple[str, ...] = ("all",)

# `scoring_format`, matching player-projections' own vocabulary.
SCORING_FORMATS: tuple[str, ...] = ("standard", "half-ppr", "ppr")

# nflverse roster position -> the fantasy position it is scored as. Anything
# absent is not a fantasy skill position and never becomes an opportunity:
# a punter's carry on a fake is real and belongs to nobody's WR matchup.
#
# Every KEY here is also a member of `player-identity`'s `KNOWN_POSITIONS`
# (see `adapters/identity.py` for why that is load-bearing rather than a
# coincidence).
ROSTER_TO_FANTASY: dict[str, str] = {
    "QB": "QB",
    "RB": "RB",
    "FB": "RB",
    "WR": "WR",
    "TE": "TE",
    "K": "K",
}

# --------------------------------------------------------------------------
# Scoring weights
# --------------------------------------------------------------------------

# The only thing `scoring_format` actually changes. Every other weight below
# is shared, which is why the K and DST rows are identical across all three
# formats -- stated rather than left to be discovered.
RECEPTION_POINTS: dict[str, float] = {
    "standard": 0.0,
    "half-ppr": 0.5,
    "ppr": 1.0,
}

YARD_POINTS = 0.1
PASS_YARD_POINTS = 0.04
PASS_TD_POINTS = 4.0
TD_POINTS = 6.0
INTERCEPTION_POINTS = -2.0
FUMBLE_LOST_POINTS = -2.0
TWO_POINT_POINTS = 2.0

# Kicking. Distance-banded, the near-universal convention. A missed field goal
# scores zero rather than a penalty: the penalty is a league setting and
# charging one here would silently understate every kicker matchup for the
# leagues that do not.
FG_BANDS: tuple[tuple[float, float], ...] = ((40.0, 3.0), (50.0, 4.0))
FG_LONG_POINTS = 5.0
EXTRA_POINT_POINTS = 1.0

# Team defense / special teams.
SACK_POINTS = 1.0
TAKEAWAY_POINTS = 2.0
SAFETY_POINTS = 2.0
DEFENSIVE_TD_POINTS = 6.0

# `(exclusive upper bound on points allowed, points)`, ascending. The standard
# ladder; anything at or above the last bound scores `POINTS_ALLOWED_FLOOR`.
POINTS_ALLOWED_TIERS: tuple[tuple[int, float], ...] = (
    (1, 10.0),
    (7, 7.0),
    (14, 4.0),
    (21, 1.0),
    (28, 0.0),
    (35, -1.0),
)
POINTS_ALLOWED_FLOOR = -4.0


def field_goal_points(distance: float) -> float:
    """Points for one made field goal of `distance` yards."""
    for bound, points in FG_BANDS:
        if distance < bound:
            return points
    return FG_LONG_POINTS


def points_allowed_points(points_allowed: int) -> float:
    """The DST points-allowed tier for a final margin against."""
    for bound, points in POINTS_ALLOWED_TIERS:
        if points_allowed < bound:
            return points
    return POINTS_ALLOWED_FLOOR


# --------------------------------------------------------------------------
# The counting stats
# --------------------------------------------------------------------------


@dataclass
class StatLine:
    """Counting stats for one (defense, player) or one (defense, position).

    Counts, never rates. Rates are computed from a *selection* of these, so
    two lines fold together with `merge` and the arithmetic happens once, at
    the end -- a mean of per-game rates would overweight a defense's low-snap
    games, and the blowouts this collector's failure mode is about are exactly
    the games whose snap counts differ most.
    """

    games: set[str] = field(default_factory=set)
    opportunities: int = 0

    targets: int = 0
    receptions: int = 0
    receiving_yards: float = 0.0
    yards_after_catch: float = 0.0
    receiving_tds: int = 0

    carries: int = 0
    rushing_yards: float = 0.0
    rushing_tds: int = 0

    pass_attempts: int = 0
    passing_yards: float = 0.0
    passing_tds: int = 0
    interceptions: int = 0

    fumbles_lost: int = 0
    two_point_conversions: int = 0

    field_goals: list[float] = field(default_factory=list)
    extra_points: int = 0

    def merge(self, other: "StatLine") -> None:
        """Fold `other` into this line, in place."""
        self.games |= other.games
        self.opportunities += other.opportunities
        self.targets += other.targets
        self.receptions += other.receptions
        self.receiving_yards += other.receiving_yards
        self.yards_after_catch += other.yards_after_catch
        self.receiving_tds += other.receiving_tds
        self.carries += other.carries
        self.rushing_yards += other.rushing_yards
        self.rushing_tds += other.rushing_tds
        self.pass_attempts += other.pass_attempts
        self.passing_yards += other.passing_yards
        self.passing_tds += other.passing_tds
        self.interceptions += other.interceptions
        self.fumbles_lost += other.fumbles_lost
        self.two_point_conversions += other.two_point_conversions
        self.field_goals.extend(other.field_goals)
        self.extra_points += other.extra_points

    def fantasy_points(self, scoring_format: str) -> float:
        """Total fantasy points in `scoring_format` units."""
        return (
            self.receptions * RECEPTION_POINTS[scoring_format]
            + (self.receiving_yards + self.rushing_yards) * YARD_POINTS
            + (self.receiving_tds + self.rushing_tds) * TD_POINTS
            + self.passing_yards * PASS_YARD_POINTS
            + self.passing_tds * PASS_TD_POINTS
            + self.interceptions * INTERCEPTION_POINTS
            + self.fumbles_lost * FUMBLE_LOST_POINTS
            + self.two_point_conversions * TWO_POINT_POINTS
            + sum(field_goal_points(distance) for distance in self.field_goals)
            + self.extra_points * EXTRA_POINT_POINTS
        )


@dataclass
class DstLine:
    """Counting stats for what one team concedes to opposing team defenses.

    **`team_id` on a `DST` row is the conceding team, and the conceding unit
    is its OFFENSE.** A defense never faces a DST, so "points a defense allows
    to the DST position" has no referent -- the spec's `team_id` gloss ("the
    defense the row describes") is the one field it cannot mean literally for
    this one enum member. The invariant that does hold for all six positions
    is *`team_id` is the team that concedes*, and a generator projecting a
    DST looks the row up by the opponent's abbreviation exactly as it does for
    every other position, so the lookup is unchanged. See README.md.
    """

    games: set[str] = field(default_factory=set)
    offensive_plays: int = 0

    sacks: int = 0
    interceptions: int = 0
    fumbles_lost: int = 0
    safeties: int = 0
    defensive_tds: int = 0
    # One entry per game: the conceding team's own final score, which is what
    # the opposing DST "allowed". Per game rather than summed, because the
    # tier ladder is not linear and a season total cannot be re-banded.
    points_scored: dict[str, int] = field(default_factory=dict)

    def merge(self, other: "DstLine") -> None:
        self.games |= other.games
        self.offensive_plays += other.offensive_plays
        self.sacks += other.sacks
        self.interceptions += other.interceptions
        self.fumbles_lost += other.fumbles_lost
        self.safeties += other.safeties
        self.defensive_tds += other.defensive_tds
        self.points_scored.update(other.points_scored)

    def fantasy_points(self, scoring_format: str) -> float:
        """Total DST fantasy points conceded.

        `scoring_format` is accepted and unused: no DST weight varies by
        format. Taking it keeps the two line types substitutable at the call
        site, and emitting three identical rows is the honest answer rather
        than three rows a caller might assume differ.
        """
        return (
            self.sacks * SACK_POINTS
            + (self.interceptions + self.fumbles_lost) * TAKEAWAY_POINTS
            + self.safeties * SAFETY_POINTS
            + self.defensive_tds * DEFENSIVE_TD_POINTS
            + sum(
                points_allowed_points(points) for points in self.points_scored.values()
            )
        )

    def to_stat_line(self) -> StatLine:
        """A `StatLine` view, so DST folds through the same rating code.

        Only `games` and `opportunities` carry over; every receiving and
        rushing counter stays zero and is published as null, because a team
        defense concedes none of them. The fantasy points are supplied
        separately -- see `ratings.build_rows`.
        """
        return StatLine(games=set(self.games), opportunities=self.offensive_plays)

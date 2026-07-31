"""The pinned scoring table, and the derived blocks computed from it.

`player-stats` is "deliberately the only collector permitted to compute fantasy
points, so scoring rules live in exactly one place". This is that place. No
adapter touches it, and no other collector may reimplement it — two
implementations of a PPR point is how a projection and a scoreboard end up
disagreeing about a week nobody can reconstruct.

`SCORING_RULES_VERSION` is emitted on every row. It exists so a stored row says
which table produced its points: change any number below and bump the version,
or a season's lake objects become silently incomparable. It is a pinned
constant rather than a date, because a rule change and a redeploy are not the
same event.

`rates` are derived and are "never an input to any sum" — they are computed
here for a reader's convenience and are `None` wherever the denominator is
zero. A rate of `0.0` for a player with no targets would be a claim about
performance; `None` is the absence of one.
"""

# Bump on any change to the table below. A row in the lake names the version
# that scored it, so two seasons under two tables stay distinguishable.
SCORING_RULES_VERSION = "2026.1"

# The three formats `player-projections` serves, and the only three this
# collector scores. Key order is the emitted key order.
FORMATS: tuple[str, ...] = ("standard", "half_ppr", "ppr")

# Points per reception, the only rule that differs across the three formats.
PPR_POINTS: dict[str, float] = {"standard": 0.0, "half_ppr": 0.5, "ppr": 1.0}

# Format-independent rules. Named constants rather than inline literals so a
# rule change is a one-line diff a reviewer can see.
POINTS_PER_PASSING_YARD = 0.04
POINTS_PER_PASSING_TD = 4.0
POINTS_PER_INTERCEPTION = -1.0
POINTS_PER_RUSHING_YARD = 0.1
POINTS_PER_RECEIVING_YARD = 0.1
POINTS_PER_NON_PASSING_TD = 6.0
POINTS_PER_RETURN_TD = 6.0
POINTS_PER_FUMBLE_LOST = -2.0
POINTS_PER_TWO_POINT_CONVERSION = 2.0

# Emitted points are rounded to this many places. Two, because a half-point
# scoring system cannot express anything finer and an unrounded float would
# make two identical box scores compare unequal in `revisions.py`.
POINT_PRECISION = 2


def _ratio(numerator: float, denominator: float) -> float | None:
    """A rate, or `None` when the denominator is zero.

    `None` rather than `0.0`: a receiver with no targets has no catch rate, and
    reporting one as zero is a claim about performance rather than the absence
    of one.
    """
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def build_rates(passing: dict, rushing: dict, receiving: dict) -> dict:
    """The `rates` block. Derived only — never an input to any sum."""
    return {
        "yards_per_attempt": _ratio(passing["yards"], passing["attempts"]),
        "catch_rate": _ratio(receiving["receptions"], receiving["targets"]),
        "yards_per_target": _ratio(receiving["yards"], receiving["targets"]),
        "yac_per_reception": _ratio(
            receiving["yards_after_catch"], receiving["receptions"]
        ),
    }


def _format_independent_points(
    passing: dict, rushing: dict, receiving: dict, misc: dict
) -> float:
    return (
        passing["yards"] * POINTS_PER_PASSING_YARD
        + passing["touchdowns"] * POINTS_PER_PASSING_TD
        + passing["interceptions"] * POINTS_PER_INTERCEPTION
        + rushing["yards"] * POINTS_PER_RUSHING_YARD
        + rushing["touchdowns"] * POINTS_PER_NON_PASSING_TD
        + receiving["yards"] * POINTS_PER_RECEIVING_YARD
        + receiving["touchdowns"] * POINTS_PER_NON_PASSING_TD
        + misc["return_touchdowns"] * POINTS_PER_RETURN_TD
        + misc["fumbles_lost"] * POINTS_PER_FUMBLE_LOST
        + misc["two_point_conversions"] * POINTS_PER_TWO_POINT_CONVERSION
    )


def build_fantasy_points(
    passing: dict, rushing: dict, receiving: dict, misc: dict
) -> dict:
    """The `fantasy_points` block: one float per scoring format.

    Computed from the counting stats, never read off the upstream. The feed
    publishes its own `fantasy_points` and `fantasy_points_ppr` columns and
    they are deliberately ignored — adopting them would move the scoring rules
    into whichever upstream happened to be wired up.
    """
    base = _format_independent_points(passing, rushing, receiving, misc)
    return {
        scoring_format: round(
            base + receiving["receptions"] * PPR_POINTS[scoring_format],
            POINT_PRECISION,
        )
        for scoring_format in FORMATS
    }

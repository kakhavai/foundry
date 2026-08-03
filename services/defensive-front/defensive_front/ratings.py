"""Counting stats -> published rows. Pure, no I/O, no roster, no network.

Four things happen here and each is a named requirement of the spec.

1. **One play set behind every rate.** `DefenseTotals` is filled from the
   intersection of play-by-play and participation — a play counts only when
   both feeds describe it. That is not tidiness: `pressure_to_sack_rate` is
   sacks over pressures, and 5.24% of charted pass-rush snaps are
   penalty-nullified `no_play` rows that play-by-play does not mark as
   dropbacks. Counting pressures on plays that can never carry a sack would
   deflate the conversion rate by that much, silently.

2. **Pressure is attributed to the rushing unit, never to the play outcome.**
   `was_pressure` is charted independently of `sack`, which is exactly the
   spec's requirement — hurries and knockdowns count even when the ball is
   out. Nothing here filters a pressure by what happened next, and
   `tests/test_ratings.py::test_a_pressure_is_counted_when_the_ball_came_out`
   is what stops one being added.

3. **The opponent adjustment is fit on the opposing OFFENCE's own production.**
   Never on a prior rating of the defence being adjusted. See
   `opponent_strengths`, whose docstring records the constant-valued bug that
   shipped on `defense-vs-position` when a unit was adjusted by its own
   leave-one-out mean of the quantity being rated.

4. **The timing-confound guard.** Owned by `timing.py`; run from here over the
   finished rows so it sees exactly what is published.
"""

import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field

from .timing import TimingRegression, residual_slope

__all__ = [
    "ADJUSTMENT_METHOD",
    "BLITZ_RUSHERS",
    "FRONT_ROTATION_SIZE",
    "FRONT_WINDOW_WEEKS",
    "MIN_OPPONENT_STRENGTH",
    "PRECISION",
    "UNITS",
    "DefenseTotals",
    "FrontTotals",
    "OffenseGameTotals",
    "build_rows",
    "continuity_index",
    "line_yards",
    "opponent_strengths",
]

# --------------------------------------------------------------------------
# The `unit` enum, narrowed to one value
# --------------------------------------------------------------------------

# **The spec declares `overall | interior | edge`. Only `overall` is emitted,
# and that is a disclosed deviation rather than an omission.**
#
# The spec's own adapter note says the split "has to come from alignment
# technique on the snap, not from the defender's listed position, or every 3-4
# outside linebacker lands in the wrong bucket". No free source publishes
# alignment technique. `pbp_participation` publishes `defense_players` (ids)
# and `defense_positions` (roster-listed) — precisely the basis the spec rules
# out. Synthesising the split from listed positions would produce two
# populated, plausible, wrong columns, which is worse than one honest one.
#
# `contracts/signal-envelope/collectors/defensive-front.json` restricts the
# enum to match and sets `additionalProperties: false`, so a row claiming
# `interior` fails conformance rather than reaching the lake.
UNITS: tuple[str, ...] = ("overall",)

# --------------------------------------------------------------------------
# Definitions that must not drift from `offensive-line`
# --------------------------------------------------------------------------

# Five or more rushers. The spec's own wording for `blitz_rate`, and the
# standard definition: four is a base rush, so the fifth is the extra man.
BLITZ_RUSHERS = 5

# **The Football Outsiders line-yards weighting**, which is what makes
# `adjusted_line_yards_allowed` comparable to the `offensive-line` field of
# the same stem. The spec is explicit that a divergence here "silently
# corrupts the differential rather than failing", so the weighting lives in
# named constants rather than inline literals and `offensive-line` must import
# the same definition when it is built.
LINE_YARDS_LOSS_CREDIT = 1.2
LINE_YARDS_FULL_MAX = 4.0
LINE_YARDS_HALF_MAX = 10.0
LINE_YARDS_HALF_CREDIT = 0.5
# Derived, never written down: a change to the weighting above cannot leave
# the cap behind.
LINE_YARDS_CAP = LINE_YARDS_FULL_MAX + LINE_YARDS_HALF_CREDIT * (
    LINE_YARDS_HALF_MAX - LINE_YARDS_FULL_MAX
)

# --------------------------------------------------------------------------
# The front rotation
# --------------------------------------------------------------------------

# **Seven, because that is what a defensive front is.** Four down linemen and
# three linebackers, or three and four — the unit is called the front seven in
# the sport, so this is a structural number rather than one fitted to a
# distribution. The index it feeds is documented against it.
FRONT_ROTATION_SIZE = 7

# How many of the most recent sampled weeks define "the current projected
# front rotation". **Three, and the reason is measured.** Taking only the most
# recent week is destroyed by week 18, where playoff-clinched teams rest
# starters: on the real 2025 season a one-week window put Green Bay at
# `0.118`, Buffalo at `0.278` and the Chargers at `0.318` — every one of them a
# rest-week artifact rather than a front that turned over. Widening to three
# weeks moves those to `0.590`, `0.589` and `0.659`, drops the cross-team
# variance 3.5x (2.34e-02 -> 6.71e-03) and lifts the league minimum from 0.118
# to 0.469. A fourth week changes the minimum not at all. Three is the
# smallest window that survives a single rest week, ejection or bye, which is
# why it is three and not a number chosen to make the output look tidy.
FRONT_WINDOW_WEEKS = 3

# --------------------------------------------------------------------------
# The opponent adjustment
# --------------------------------------------------------------------------

# `adjustment_method`. It names what this module actually does rather than a
# model it does not implement: leave-one-out, fit on the opposing offence's
# own production, applied multiplicatively against the league mean. Bump it if
# the arithmetic changes — a consumer reading an old lake object has nothing
# else to tell it which vintage it holds.
ADJUSTMENT_METHOD = "opposing_offense_production_ratio_loo_v1"

# Below this, the opponent-strength index is not trusted to divide by: a slate
# of offences that produced essentially nothing would otherwise turn a small
# raw rate into an enormous adjusted one. Below the floor the raw figure is
# published unchanged.
MIN_OPPONENT_STRENGTH = 0.05

# Four places is past any meaningful precision in a per-snap rate and keeps the
# lake object from carrying seventeen digits of float noise that differ between
# runs for no reason.
PRECISION = 4


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, PRECISION)


def _rate(numerator: float, denominator: float) -> float | None:
    """A rate, or `None` when its denominator is zero.

    `None` rather than `0.0`: "this front generated no pressure when blitzing"
    and "this front never blitzed" are different facts, and a generator
    averaging the second as a zero would rate a front that never blitzed as
    the worst blitzing front in the league.
    """
    return numerator / denominator if denominator else None


def line_yards(rushing_yards: float) -> float:
    """One carry's line-attributed yardage, on the Football Outsiders scale.

    120% credit to the front for a run stopped behind the line, full credit
    through four yards, half credit from five to ten, nothing past ten — the
    yards a back gains in the open field are his, not the line's. Capped at
    `LINE_YARDS_CAP`, which is derived from the bands above.
    """
    if rushing_yards < 0.0:
        return LINE_YARDS_LOSS_CREDIT * rushing_yards
    if rushing_yards <= LINE_YARDS_FULL_MAX:
        return rushing_yards
    if rushing_yards <= LINE_YARDS_HALF_MAX:
        return LINE_YARDS_FULL_MAX + LINE_YARDS_HALF_CREDIT * (
            rushing_yards - LINE_YARDS_FULL_MAX
        )
    return LINE_YARDS_CAP


@dataclass
class DefenseTotals:
    """One defence's counting stats over the sampled window."""

    pass_rush_snaps: int = 0
    pressures: int = 0
    sacks: int = 0
    blitzes: int = 0
    blitz_pressures: int = 0
    time_to_throw_sum: float = 0.0
    time_to_throw_charted: int = 0
    carries: int = 0
    line_yards: float = 0.0
    stuffs: int = 0


@dataclass
class OffenseGameTotals:
    """What one offence produced in one game — the adjustment's yardstick.

    Keyed by the **producing** unit, which for every metric here is the
    offence. See `opponent_strengths` for what happens when a caller keys it
    by the conceding unit instead.
    """

    pass_rush_snaps: int = 0
    pressures_allowed: int = 0
    sacks_allowed: int = 0
    carries: int = 0
    line_yards_allowed: float = 0.0


@dataclass
class FrontTotals:
    """Everything `build_rows` needs, already folded and joined."""

    defense: dict[str, DefenseTotals] = field(default_factory=dict)
    offense_game: dict[tuple[str, str], OffenseGameTotals] = field(default_factory=dict)
    # `(team, game_id) -> the other team`, built from the same plays as
    # `defense`, so the adjustment cannot be fit against a different schedule
    # than the rates were computed over.
    opponents: dict[tuple[str, str], str] = field(default_factory=dict)
    weeks: set[int] = field(default_factory=set)
    # `defense -> gsis_id -> pass-rush snaps`, already narrowed to front
    # players by the caller. Empty when the roster feed was unavailable.
    front_snaps: dict[str, dict[str, int]] = field(default_factory=dict)
    # The same, restricted to the last `FRONT_WINDOW_WEEKS` sampled weeks.
    recent_front_snaps: dict[str, dict[str, int]] = field(default_factory=dict)

    def teams(self) -> set[str]:
        """Every team seen on either side of the ball."""
        found = {team for team, _game in self.opponents}
        return found | set(self.opponents.values())


def opponent_strengths(
    production: Mapping[tuple[str, str], float],
) -> dict[tuple[str, str], float]:
    """`(offence, game) -> that offence's strength, excluding that game`.

    **Fit on the producing unit's own output and nothing else** — never on any
    prior rating of the defence being adjusted. A rating is itself a function
    of the units faced, so adjusting a defence by its opponents' *defensive*
    ratings feeds the quantity straight back into its own estimate.

    **Leave-one-out.** An offence's strength, as used to adjust the defence it
    played, is computed from that offence's OTHER games. Without it the game
    being adjusted appears on both sides: a front that flattened an offence
    would be told that offence is weak partly *because* of the flattening, and
    would have its own achievement adjusted away. An offence with exactly one
    game has no other games, so it falls back to that game.

    **A caller that hands this the CONCEDING unit rather than the producing
    one gets a silent constant, not an error.** The mean of a unit's
    leave-one-out means is exactly its full mean, so the strength becomes
    `own_rate / league_mean` and `rate / strength` collapses to the league mean
    identically, for every team. That shipped once on `defense-vs-position`:
    all 32 DST rows published the same number while the raw values spanned
    2.588 to 10.471 — every field populated, every value plausible, and the
    column carrying no information at all. **The tell was zero variance in the
    adjusted column**, which is why `metrics.adjusted_variance` records it on
    every pass and `tests/test_ratings.py` asserts it is non-zero against a
    fixture built as `f(offence) + g(defence)`.

    Strength is relative to the league mean, so `1.0` is average — the unit
    `opponent_pressure_strength_index` is documented in.
    """
    if not production:
        return {}
    league_mean = statistics.fmean(production.values())
    if league_mean <= 0:
        # Nobody produced anything at all — a genuine possibility in week 1 of
        # a metric with a sparse denominator. Every unit is average.
        return dict.fromkeys(production, 1.0)

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for (unit, _game), value in production.items():
        totals[unit] = totals.get(unit, 0.0) + value
        counts[unit] = counts.get(unit, 0) + 1

    strengths: dict[tuple[str, str], float] = {}
    for (unit, game), value in production.items():
        remaining = counts[unit] - 1
        mean = totals[unit] if remaining <= 0 else (totals[unit] - value) / remaining
        strengths[(unit, game)] = mean / league_mean
    return strengths


def _faced_strength(
    team: str,
    strengths: Mapping[tuple[str, str], float],
    opponents: Mapping[tuple[str, str], str],
) -> float:
    """The mean strength of the offences `team` actually faced.

    **Re-keyed onto the opponent**, with no per-metric exception. That single
    rule is the whole fix for the bug in `opponent_strengths`' docstring: the
    exception was precisely the defect.
    """
    samples = [
        strengths[(opponent, game)]
        for (defense, game), opponent in opponents.items()
        if defense == team and (opponent, game) in strengths
    ]
    return statistics.fmean(samples) if samples else 1.0


def _adjust(raw: float | None, strength: float) -> float | None:
    if raw is None or strength < MIN_OPPONENT_STRENGTH:
        return raw
    return raw / strength


def continuity_index(
    window_snaps: Mapping[str, int],
    recent_snaps: Mapping[str, int],
) -> float | None:
    """Share of the window's front snaps taken by the CURRENT front rotation.

    The rotation is the `FRONT_ROTATION_SIZE` front defenders with the most
    snaps over the last `FRONT_WINDOW_WEEKS` sampled weeks — who is playing
    now. The index is their combined share of every front-player snap in the
    whole sampled window: `1.0` means nobody outside the current seven took a
    front snap all window, and a low value means the front that will play next
    week is not the front the rest of this row describes.

    The denominator is the window's **actual** front participations, not
    `FRONT_ROTATION_SIZE x pass_rush_snaps`. Nickel is the modern base defence
    and drops a linebacker, so a snap carries about 6.3 front players rather
    than 7; dividing by seven would deflate every team by ~10% for a reason
    that has nothing to do with continuity.

    `None` when the window has no front snaps at all — which is what a missing
    roster feed looks like, and is a different fact from a front that turned
    over completely.
    """
    total = sum(window_snaps.values())
    if not total or not recent_snaps:
        return None
    # Ties broken by id, not by dict order: two rotational ends on identical
    # snap counts would otherwise land in the rotation depending on which
    # game streamed first, and the index would move between passes over
    # nothing.
    ranked = sorted(recent_snaps.items(), key=lambda item: (-item[1], item[0]))
    rotation = [player for player, _snaps in ranked[:FRONT_ROTATION_SIZE]]
    return sum(window_snaps.get(player, 0) for player in rotation) / total


def build_rows(
    totals: FrontTotals,
    *,
    absences: Mapping[str, list[str]],
    degraded: tuple[str, ...],
    null_field_reason: Mapping[str, str],
) -> tuple[list[dict], TimingRegression | None]:
    """Every published row, plus this pass's timing-confound verdict.

    One row per defence at `unit: overall` — see `UNITS` for why the spec's
    other two values are not emitted.
    """
    teams = sorted(totals.teams())
    window_weeks = len(totals.weeks)

    pressure_production = {
        key: rate
        for key, line in totals.offense_game.items()
        if (rate := _rate(line.pressures_allowed, line.pass_rush_snaps)) is not None
    }
    sack_production = {
        key: rate
        for key, line in totals.offense_game.items()
        if (rate := _rate(line.sacks_allowed, line.pass_rush_snaps)) is not None
    }
    line_yard_production = {
        key: rate
        for key, line in totals.offense_game.items()
        if (rate := _rate(line.line_yards_allowed, line.carries)) is not None
    }
    pressure_strength = opponent_strengths(pressure_production)
    sack_strength = opponent_strengths(sack_production)
    line_yard_strength = opponent_strengths(line_yard_production)

    staged: list[dict] = []
    for team in teams:
        line = totals.defense.get(team, DefenseTotals())
        faced_pressure = _faced_strength(team, pressure_strength, totals.opponents)
        faced_sack = _faced_strength(team, sack_strength, totals.opponents)
        faced_line_yards = _faced_strength(team, line_yard_strength, totals.opponents)

        pressure_rate = _rate(line.pressures, line.pass_rush_snaps)
        sack_rate = _rate(line.sacks, line.pass_rush_snaps)
        raw_line_yards = _rate(line.line_yards, line.carries)

        staged.append(
            {
                "team_id": team,
                "unit": UNITS[0],
                "pass_rush_snaps": line.pass_rush_snaps,
                "run_defense_snaps": line.carries,
                "pressure_rate_generated": _round(pressure_rate),
                "pressure_rate_generated_adj": _round(
                    _adjust(pressure_rate, faced_pressure)
                ),
                "sack_rate_generated": _round(sack_rate),
                "sack_rate_generated_adj": _round(_adjust(sack_rate, faced_sack)),
                # Sacks over PRESSURES, not over dropbacks: the finishing
                # component. Both come from the same joined play set.
                "pressure_to_sack_rate": _round(_rate(line.sacks, line.pressures)),
                "blitz_rate": _round(_rate(line.blitzes, line.pass_rush_snaps)),
                "pressure_rate_when_blitzing": _round(
                    _rate(line.blitz_pressures, line.blitzes)
                ),
                "mean_time_to_throw_faced": _round(
                    _rate(line.time_to_throw_sum, line.time_to_throw_charted)
                ),
                "run_stuff_rate_generated": _round(_rate(line.stuffs, line.carries)),
                # Null by necessity, with the reason on the row. See
                # `null_field_reason` and this collector's README.
                "yards_before_contact_allowed_per_carry": None,
                "yards_before_contact_allowed_per_carry_adj": None,
                "adjusted_line_yards_allowed": _round(
                    _adjust(raw_line_yards, faced_line_yards)
                ),
                "front_continuity_index": _round(
                    continuity_index(
                        totals.front_snaps.get(team, {}),
                        totals.recent_front_snaps.get(team, {}),
                    )
                ),
                "key_absences": list(absences.get(team, ())),
                "opponent_pressure_strength_index": _round(faced_pressure),
                "adjustment_method": ADJUSTMENT_METHOD,
                "adjustment_window_weeks": window_weeks,
                "degraded_upstreams": list(degraded),
                "null_field_reason": dict(null_field_reason),
            }
        )

    # --- The guard, over exactly what is about to be published -------------
    #
    # Only teams with BOTH a charted release time and an adjusted rate can be
    # regressed; a team missing either is not comparable and is dropped from
    # both sides together, the same way `defense-vs-position` ranks only the
    # intersection rather than disabling its guard for the whole league.
    pairs = [
        (row["mean_time_to_throw_faced"], row["pressure_rate_generated_adj"])
        for row in staged
        if row["mean_time_to_throw_faced"] is not None
        and row["pressure_rate_generated_adj"] is not None
    ]
    regression = residual_slope([x for x, _ in pairs], [y for _, y in pairs])

    # **Two booleans, not one.** `timing_confound_flagged: false` alone
    # conflates "the guard ran and found nothing" with "the guard could not
    # run", and the row is what a generator joins on. Everywhere else this
    # collector keeps that distinction — the `defensive_front_timing_guard_ran`
    # gauge, the `NOT_RUN` sentinels, the `timing_guard_could_not_run` coverage
    # error — and the row was the one place it was dropped.
    ran = regression is not None
    flagged = ran and regression.flagged
    for row in staged:
        row["timing_guard_ran"] = ran
        row["timing_confound_flagged"] = flagged
    return staged, regression

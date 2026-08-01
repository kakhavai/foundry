"""Turning counting stats into published rows. Pure, no I/O.

Three things happen here and each one is a named requirement of the spec:

1. **Both bases from one play set.** `per_game` is the single accumulator
   everything below reads. `fantasy_points_allowed_per_game` and
   `fantasy_points_allowed_per_opportunity` are the same numerator over two of
   its denominators, so there is no arrangement of this module in which they
   describe different sets of plays.

2. **The opponent adjustment is fit on offensive units.** Never on prior
   defensive ratings -- that is the spec's explicit prohibition, and it is not
   stylistic: a defensive rating is itself a function of the offenses faced,
   so adjusting a defense by its opponents' *defensive* ratings feeds the
   quantity back into its own estimate. See `offense_strengths`.

3. **The rank-divergence guard.** See `divergent_teams` for the failure it
   catches, why a null check cannot, and what the live rate is.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .scoring import (
    ALIGNMENTS,
    PLAYER_POSITIONS,
    POSITIONS,
    SCORING_FORMATS,
    DstLine,
    StatLine,
)

# --------------------------------------------------------------------------
# The rank-divergence guard
# --------------------------------------------------------------------------

# **The spec's number. Do not calibrate it to today's distribution.**
#
# "flag any team whose two ranks differ by more than eight places for manual
# review before the row is published." A threshold fitted to the data in front
# of you stops being a guard and becomes a filter on tomorrow's signal --
# `team-scheme` rejected a plausibility bound for exactly that reason. Eight
# places out of thirty-two is ~25% of the league, which is a large move by
# construction rather than by tuning.
#
# **Checked against a shuffled null before it was trusted**, because a
# statistic can be dead: `coaching-scheme`'s changepoint detector fired on 65%
# of teams against a 55% null and shipped disabled. Measured on the real 2025
# regular season through this exact code path (see README.md for the table):
#
#   position   flagged/32     shuffled null    ratio
#   QB          2  ( 6.2%)         54.6%        0.11
#   RB         3-4 (10.4%)         53.9%        0.19
#   WR         6-7 (19.8%)         54.2%        0.37
#   TE         6-8 (21.9%)         53.6%        0.41
#   K          12  (37.5%)         54.7%        0.69
#   DST         0  ( 0.0%)         54.2%        0.00
#   ALL        92/576 (16.0%)      54.2%        0.29
#
# The null is two independent rankings of 32 teams, which flags 54% of them by
# construction. Every position beats it, so the guard carries information
# rather than noise -- and on the two positions the spec's failure mode
# actually names (WR, TE) it flags six to eight, not twenty. **K is the weak
# one at 0.69** and is called out in README.md rather than quietly tuned away.
#
# It also fires in the right DIRECTION: on WR/ppr, BAL ranks 4th-most-allowed
# per game and 23rd per opportunity, IND 2nd and 19th. That is the spec's
# sentence exactly -- "the raw rating reads as a soft matchup precisely for
# the teams that are hardest to score against."
RANK_DIVERGENCE_THRESHOLD = 8.0

# `adjustment_method`, and it names what this module actually does rather than
# a model it does not implement. "loo" is leave-one-out: an opponent's
# offensive strength is computed with the game in question excluded, so the
# defense being rated does not contribute to the yardstick it is measured
# against. Bump the suffix if the arithmetic changes -- it is the only thing
# telling a consumer which vintage of the adjustment produced a stored row.
ADJUSTMENT_METHOD = "opponent_offense_mean_ratio_loo_v1"

# Below this, a defense's opponent-strength index is not trusted to divide by:
# a schedule of offenses that scored essentially nothing at a position would
# otherwise turn a small raw allowance into an enormous adjusted one.
MIN_OPPONENT_STRENGTH = 0.05

# Published floats are rounded here rather than at the schema. Four places is
# past any meaningful precision in a per-game fantasy rate and keeps the lake
# object from carrying seventeen digits of float noise that differ between
# runs for no reason.
PRECISION = 4


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, PRECISION)


def average_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Descending ranks, ties averaged. Rank 1 is the largest value.

    Ties are averaged rather than broken by key order. With counting stats
    over a short season, exact ties are common at the bottom of a split (every
    defense that faced zero opportunities scores 0.0), and breaking them
    alphabetically would manufacture a rank spread of up to the width of the
    tie group -- which the divergence check would then read as a real
    disagreement between the two bases.
    """
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    ranks: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        # 1-based, averaged across the tie group.
        shared = (index + end) / 2 + 1
        for position in range(index, end + 1):
            ranks[ordered[position][0]] = shared
        index = end + 1
    return ranks


def divergent_teams(
    per_game: Mapping[str, float],
    per_opportunity: Mapping[str, float],
    *,
    threshold: float = RANK_DIVERGENCE_THRESHOLD,
) -> dict[str, float]:
    """`team -> |rank difference|` for every team past `threshold`.

    **The failure this catches, and why a null check cannot.** Defenses that
    build leads face pass-heavy opponents in the fourth quarter, so a strong
    defense accumulates an inflated per-game WR and TE allowance while its
    per-opportunity allowance stays elite. Every field is populated, every
    value is plausible, and the raw rating reads as a soft matchup precisely
    for the teams that are hardest to score against. Nothing about the row is
    null, malformed or out of range -- the two numbers simply disagree about
    the same defense, and only comparing them across the league can see it.

    `threshold` is a parameter so a test can drive both arms, **not** so a
    deployment can tune it. The default is the spec's eight and the only value
    the collector ever passes.

    Returns the flagged teams, keyed. Flagged, never dropped: the row is
    published with `rank_divergence_flagged: true`, an entry in
    `coverage.errors`, and a counter -- so a consumer can discount it and an
    operator can go and look, which is what "for manual review" asks for.
    """
    if not per_game or set(per_game) != set(per_opportunity):
        # Two different populations cannot be rank-compared. Nothing in this
        # module produces that, so it is a guard against a future caller
        # rather than a case with a fixture.
        return {}
    game_ranks = average_ranks(per_game)
    opportunity_ranks = average_ranks(per_opportunity)
    flagged: dict[str, float] = {}
    for team, rank in game_ranks.items():
        gap = abs(rank - opportunity_ranks[team])
        if gap > threshold:
            flagged[team] = gap
    return flagged


# --------------------------------------------------------------------------
# The opponent adjustment
# --------------------------------------------------------------------------


def offense_strengths(
    game_points: Mapping[tuple[str, str], float],
) -> dict[tuple[str, str], float]:
    """`(offense, game) -> that offense's strength, excluding that game`.

    **Fit on offensive units and nothing else.** The input is per-game fantasy
    production BY an offense at one position -- not any defense's rating, and
    not any quantity derived from one. The spec forbids the alternative
    outright, and the reason is circularity: a defensive rating is already a
    function of the offenses it faced, so adjusting defense D by its
    opponents' defensive ratings puts D's own allowance back into its own
    correction term through however many hops the schedule provides.

    **Leave-one-out.** An opponent's strength, as used to adjust the defense
    it played, is computed from that opponent's OTHER games. Without it the
    game being adjusted appears on both sides -- a defense that shut an
    offense out would be told that offense is weak partly because of the
    shutout, and would have its own achievement adjusted away. An offense with
    exactly one game has no other games, so it falls back to that game and the
    residual circularity is stated rather than hidden.

    Strength is relative to the league mean per game, so `1.0` is average --
    which is the unit `opponent_strength_index` is documented in.
    """
    if not game_points:
        return {}
    league_mean = sum(game_points.values()) / len(game_points)
    if league_mean <= 0:
        # Nobody produced anything at this position all season (a genuine
        # possibility for a bye-heavy early week). Every offense is average.
        return dict.fromkeys(game_points, 1.0)

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for (offense, _game), points in game_points.items():
        totals[offense] = totals.get(offense, 0.0) + points
        counts[offense] = counts.get(offense, 0) + 1

    strengths: dict[tuple[str, str], float] = {}
    for (offense, game), points in game_points.items():
        remaining = counts[offense] - 1
        if remaining <= 0:
            mean = totals[offense]
        else:
            mean = (totals[offense] - points) / remaining
        strengths[(offense, game)] = mean / league_mean
    return strengths


# --------------------------------------------------------------------------
# Row construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """One published row's identity: the four dimensions that key it."""

    team_id: str
    position: str
    alignment: str
    scoring_format: str


@dataclass
class SeasonTotals:
    """Everything `build_rows` needs, already attributed and resolved.

    `per_game` is keyed `(defense, position, game_id)` and is the single
    source both rate bases and the whole opponent adjustment read from.
    `dst_per_game` is its team-defense counterpart, keyed `(team, game_id)`,
    where `team` is the CONCEDING team -- see `DstLine`.
    """

    per_game: dict[tuple[str, str, str], StatLine]
    dst_per_game: dict[tuple[str, str], DstLine]
    # `(team, game_id) -> the other team`. Built from the same rows as
    # `per_game`, so the adjustment cannot be fit against a different schedule
    # than the ratings were computed over.
    opponents: dict[tuple[str, str], str]
    weeks: set[int]

    def teams(self) -> set[str]:
        """Every team this pass saw, on either side of the ball."""
        found = {team for team, _game in self.opponents}
        found |= {opponent for opponent in self.opponents.values()}
        return found


def _rate(numerator: float, denominator: float) -> float | None:
    """A rate, or `None` when its denominator is zero.

    `None` rather than `0.0`: "this defense conceded zero yards after the
    catch per reception" and "this defense faced no receptions" are different
    facts, and a generator averaging the second as a zero would rate an unseen
    split as elite.
    """
    return numerator / denominator if denominator else None


def build_rows(
    totals: SeasonTotals,
) -> tuple[list[dict], dict[Split, float]]:
    """Every published row, plus the rank divergences the guard flagged.

    Returns `(rows, flagged)`. `flagged` is keyed by the split whose row was
    flagged, valued with the rank gap, so the caller can file one coverage
    error per flag and record the count. The rows themselves carry
    `rank_divergence_flagged` so the flag survives into the lake and reaches
    the generator, rather than living only in an errors array a consumer of
    `/signals` may never read.
    """
    teams = sorted(totals.teams())
    adjustment_window = len(totals.weeks)
    rows: list[dict] = []
    flagged: dict[Split, float] = {}

    for position in POSITIONS:
        # Per-game lines for this position, indexed by defense and by game.
        if position == "DST":
            lines = {
                (team, game): line.to_stat_line()
                for (team, game), line in totals.dst_per_game.items()
            }
            points_of = {
                (team, game): line.fantasy_points
                for (team, game), line in totals.dst_per_game.items()
            }
        else:
            lines = {
                (defense, game): line
                for (defense, pos, game), line in totals.per_game.items()
                if pos == position
            }
            points_of = {key: line.fantasy_points for key, line in lines.items()}

        season: dict[str, StatLine] = {}
        for (team, _game), line in lines.items():
            season.setdefault(team, StatLine()).merge(line)

        for scoring_format in SCORING_FORMATS:
            # The offensive-unit yardstick: what each OFFENSE produced at this
            # position, per game. Derived from the same `lines` -- the offense
            # in a defense's game is that defense's opponent.
            offense_points: dict[tuple[str, str], float] = {}
            for (defense, game), points in points_of.items():
                if position == "DST":
                    # A DST line is already keyed by the conceding OFFENSE.
                    offense_points[(defense, game)] = points(scoring_format)
                else:
                    opponent = totals.opponents.get((defense, game))
                    if opponent is not None:
                        offense_points[(opponent, game)] = points(scoring_format)
            strengths = offense_strengths(offense_points)

            per_game_rate: dict[str, float] = {}
            per_opportunity_rate: dict[str, float] = {}
            staged: dict[str, dict] = {}

            for team in teams:
                line = season.get(team, StatLine())
                games_sampled = len(line.games)
                points = sum(
                    points_of[(team, game)](scoring_format) for game in line.games
                )
                strength_samples = [
                    strengths[(opponent, game)]
                    for game in line.games
                    if position != "DST"
                    and (opponent := totals.opponents.get((team, game))) is not None
                    and (opponent, game) in strengths
                ]
                if position == "DST":
                    # A DST row's opponent is the defense that faced this
                    # team's offense; the yardstick is that team's OWN
                    # offensive strength, which is what `strengths` already
                    # holds for it.
                    strength_samples = [
                        strengths[(team, game)]
                        for game in line.games
                        if (team, game) in strengths
                    ]
                strength = (
                    sum(strength_samples) / len(strength_samples)
                    if strength_samples
                    else 1.0
                )

                ppg = _rate(points, games_sampled)
                ppo = _rate(points, line.opportunities)
                if ppg is not None:
                    per_game_rate[team] = ppg
                if ppo is not None:
                    per_opportunity_rate[team] = ppo

                adjusted = (
                    ppg / strength
                    if ppg is not None and strength >= MIN_OPPONENT_STRENGTH
                    else ppg
                )
                receiving = position in {"QB", "RB", "WR", "TE"}
                staged[team] = {
                    "team_id": team,
                    "position": position,
                    "scoring_format": scoring_format,
                    "games_sampled": games_sampled,
                    "opportunities_defended": line.opportunities,
                    "fantasy_points_allowed_per_game": _round(ppg),
                    "fantasy_points_allowed_per_game_adj": _round(adjusted),
                    "fantasy_points_allowed_per_opportunity": _round(ppo),
                    "targets_allowed_per_game": (
                        _round(_rate(line.targets, games_sampled))
                        if receiving
                        else None
                    ),
                    "receptions_allowed_per_game": (
                        _round(_rate(line.receptions, games_sampled))
                        if receiving
                        else None
                    ),
                    "receiving_yards_allowed_per_game": (
                        _round(_rate(line.receiving_yards, games_sampled))
                        if receiving
                        else None
                    ),
                    "yac_allowed_per_reception": (
                        _round(_rate(line.yards_after_catch, line.receptions))
                        if receiving
                        else None
                    ),
                    # Spec: "Populated for RB alignments; null otherwise."
                    "rush_yards_allowed_per_carry": (
                        _round(_rate(line.rushing_yards, line.carries))
                        if position == "RB"
                        else None
                    ),
                    "touchdowns_allowed_per_game": _round(
                        _rate(
                            line.receiving_tds + line.rushing_tds + line.passing_tds,
                            games_sampled,
                        )
                    ),
                    "opponent_strength_index": _round(strength),
                    "adjustment_method": ADJUSTMENT_METHOD,
                    "adjustment_window_weeks": adjustment_window,
                }

            divergences = divergent_teams(per_game_rate, per_opportunity_rate)
            for alignment in ALIGNMENTS:
                for team in teams:
                    row = dict(staged[team])
                    row["alignment"] = alignment
                    gap = divergences.get(team)
                    row["rank_divergence_flagged"] = gap is not None
                    if gap is not None:
                        flagged[Split(team, position, alignment, scoring_format)] = gap
                    rows.append(row)

    return rows, flagged


def declared_splits() -> int:
    """How many (position, alignment, scoring_format) rows each defense owes.

    The `/catalog`-declared split count coverage's `present` predicate tests
    against. Derived from the three dimension tuples rather than written down,
    so adding a scoring format or unlocking an alignment cannot leave the
    coverage predicate behind.
    """
    return len(POSITIONS) * len(ALIGNMENTS) * len(SCORING_FORMATS)


__all__ = [
    "ADJUSTMENT_METHOD",
    "PLAYER_POSITIONS",
    "RANK_DIVERGENCE_THRESHOLD",
    "SeasonTotals",
    "Split",
    "average_ranks",
    "build_rows",
    "declared_splits",
    "divergent_teams",
    "offense_strengths",
]

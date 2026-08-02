"""Turning counting stats into published rows. Pure, no I/O.

Three things happen here and each one is a named requirement of the spec:

1. **Both bases from one play set.** `per_game` is the single accumulator
   everything below reads. `fantasy_points_allowed_per_game` and
   `fantasy_points_allowed_per_opportunity` are the same numerator over two of
   its denominators, so there is no arrangement of this module in which they
   describe different sets of plays.

2. **The opponent adjustment is fit on the opposing unit's own production.**
   Never on a prior rating of the unit being adjusted -- that is the spec's
   explicit prohibition, and it is not stylistic: a rating is itself a
   function of the units faced, so adjusting a defense by its opponents'
   *defensive* ratings feeds the quantity back into its own estimate. For the
   five player positions the opposing unit is an offense, which is the spec's
   wording exactly; for `DST` it is the opposing defense, because that is what
   a conceding offense faced. See `opposing_unit_strengths`, whose docstring
   also records the constant-valued bug that shipped when `DST` skipped the
   re-key onto its opponent.

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
# a model it does not implement. "loo" is leave-one-out: the opposing unit's
# strength is computed with the game in question excluded, so the unit being
# rated does not contribute to the yardstick it is measured against.
#
# **v2, and the bump is load-bearing rather than cosmetic.** v1 rows are in
# the lake carrying a `DST` adjusted column that is the league mean for every
# team -- `build_rows` skipped the re-key onto the producing opponent for that
# one position, which made the yardstick self-referential. A consumer has to
# be able to tell a v1 DST row from a v2 one, and this field is the only thing
# that can tell it. "offense" also left the name: for `DST` the opposing unit
# is a defense. Bump again if the arithmetic changes again.
ADJUSTMENT_METHOD = "opponent_unit_mean_ratio_loo_v2"

# Below this, the opponent-strength index is not trusted to divide by: a
# schedule of units that produced essentially nothing at a position would
# otherwise turn a small raw allowance into an enormous adjusted one. Below
# the floor the raw figure is published unchanged.
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

    **A team missing from either basis is excluded, not a reason to give
    up on the split.** The two maps admit a team only where its rate is
    non-`None`, so one team with games but zero opportunities used to make the
    key sets unequal and disable the guard for **all 32 teams** of that
    position and scoring format -- silently, with the divergence gauge
    recording a perfectly plausible zero. That is reachable: the fumble branch
    in `_fold_players` adds a game to `games` without incrementing
    `opportunities`, so a player whose only involvement in a game was a lost
    fumble produces exactly such a line.

    Ranking the intersection keeps the guard running on every team that CAN be
    compared, which is strictly more than none. A team that cannot be ranked on
    both bases is genuinely not comparable and is dropped from both rankings
    together, so it cannot shift anyone else's rank either.
    """
    common = set(per_game) & set(per_opportunity)
    if not common:
        return {}
    game_ranks = average_ranks({team: per_game[team] for team in common})
    opportunity_ranks = average_ranks({team: per_opportunity[team] for team in common})
    flagged: dict[str, float] = {}
    for team, rank in game_ranks.items():
        gap = abs(rank - opportunity_ranks[team])
        if gap > threshold:
            flagged[team] = gap
    return flagged


# --------------------------------------------------------------------------
# The opponent adjustment
# --------------------------------------------------------------------------


def opposing_unit_strengths(
    game_points: Mapping[tuple[str, str], float],
) -> dict[tuple[str, str], float]:
    """`(unit, game) -> that unit's strength, excluding that game`.

    **Fit on the PRODUCING unit's own output and nothing else** -- never on
    any prior rating of the unit being adjusted. For the five player positions
    the producing unit is an opposing offense, which is the spec's "fit the
    opponent adjustment on offensive units". For `DST` it is an opposing
    defense's own sack, takeaway and return-touchdown generation, because that
    is the unit a conceding offense actually faced.

    The spec's prohibition -- "never on prior defensive ratings" -- is written
    for the rows that describe a defense, where the opposing unit IS an
    offense. What generalises to all six positions is the rule this function
    enforces structurally by taking exactly one argument: **production, never
    ratings**. The circularity being avoided is identical in both directions,
    because a rating is already a function of the units it faced, so adjusting
    by ratings feeds the quantity back into its own estimate through however
    many hops the schedule provides.

    **Leave-one-out.** A unit's strength, as used to adjust the unit it played,
    is computed from that unit's OTHER games. Without it the game being
    adjusted appears on both sides -- a defense that shut an offense out would
    be told that offense is weak partly because of the shutout, and would have
    its own achievement adjusted away. A unit with exactly one game has no
    other games, so it falls back to that game and the residual is stated
    rather than hidden.

    **A caller that hands this the CONCEDING unit rather than the producing
    one gets a silent constant, not an error.** The mean of a team's
    leave-one-out means is exactly its full mean, so the strength becomes
    `own_rate / league_mean` and `rate / strength` collapses to the league mean
    identically, for every team. That shipped once for `DST`; see the re-keying
    comment in `build_rows`.

    Strength is relative to the league mean per game, so `1.0` is average --
    which is the unit `opponent_strength_index` is documented in.
    """
    if not game_points:
        return {}
    league_mean = sum(game_points.values()) / len(game_points)
    if league_mean <= 0:
        # Nobody produced anything at this position all season (a genuine
        # possibility for a bye-heavy early week). Every unit is average.
        return dict.fromkeys(game_points, 1.0)

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for (unit, _game), points in game_points.items():
        totals[unit] = totals.get(unit, 0.0) + points
        counts[unit] = counts.get(unit, 0) + 1

    strengths: dict[tuple[str, str], float] = {}
    for (unit, game), points in game_points.items():
        remaining = counts[unit] - 1
        if remaining <= 0:
            mean = totals[unit]
        else:
            mean = (totals[unit] - points) / remaining
        strengths[(unit, game)] = mean / league_mean
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
            # The yardstick: what the OPPOSING unit produced, per game.
            #
            # **Every line in `points_of` is keyed by the unit that CONCEDED,
            # so every one of them is re-keyed onto the unit that PRODUCED.**
            # That is one rule with no per-position exception, and the
            # exception is precisely the bug it replaces: `DST` lines were
            # left keyed by the conceding team, which made `strengths` that
            # team's own leave-one-out mean of the very quantity being rated.
            # The mean of a team's leave-one-out means is exactly its full
            # mean, so `adjusted` collapsed to the league mean *identically* --
            # all 32 DST rows published the same number while the raw values
            # spanned 2.588 to 10.471. Every field populated, every value
            # plausible, and the column carried no information at all.
            #
            # Re-keyed, the producing unit is the opponent in both cases: an
            # opposing OFFENSE for the five player positions, an opposing
            # DEFENSE's takeaway/sack/return generation for `DST`. The spec's
            # "fit on offensive units, never on prior defensive ratings" is
            # written for the rows that describe a defense, where the opposing
            # unit IS an offense; the invariant that generalises to all six is
            # *adjust by the opposing unit's own production, never by any
            # prior rating of the unit being rated*. Leave-one-out is what
            # keeps that non-circular, identically in both directions.
            unit_points: dict[tuple[str, str], float] = {}
            for (conceded_by, game), points in points_of.items():
                produced_by = totals.opponents.get((conceded_by, game))
                if produced_by is not None:
                    unit_points[(produced_by, game)] = points(scoring_format)
            strengths = opposing_unit_strengths(unit_points)

            per_game_rate: dict[str, float] = {}
            per_opportunity_rate: dict[str, float] = {}
            staged: dict[str, dict] = {}

            for team in teams:
                line = season.get(team, StatLine())
                games_sampled = len(line.games)
                points = sum(
                    points_of[(team, game)](scoring_format) for game in line.games
                )
                # The opposing unit's strength, in every one of this team's
                # sampled games. No per-position branch -- see the re-keying
                # comment above for why the branch that used to be here made
                # the DST adjustment self-referential.
                strength_samples = [
                    strengths[(opponent, game)]
                    for game in line.games
                    if (opponent := totals.opponents.get((team, game))) is not None
                    and (opponent, game) in strengths
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
    "opposing_unit_strengths",
]

"""Counting stats -> published rows. Pure, no I/O, no roster fetch, no network.

Five things happen here and each is a named requirement of the spec.

1. **One play set behind every rate.** `LineTotals` is filled from the
   intersection of play-by-play and participation — a play counts only when
   both feeds describe it. Identical to `defensive-front`'s join, because the
   pair has to be differenced without conversion: `pass_block_snaps` here and
   `pass_rush_snaps` there are the **same number** counted from opposite
   sides of the ball.

2. **Scale agreement with `defensive-front` is a constant, not a convention.**
   The spec: "any divergence silently corrupts the differential rather than
   failing". So every shared definition below (`LINE_YARDS_*`,
   `MIN_OPPONENT_STRENGTH`, `PRECISION`, the leave-one-out adjustment's
   arithmetic) is written as a named constant and
   `tests/test_scale_agreement.py` reads `defensive-front`'s own module **by
   AST** and asserts each one matches — a demonstration rather than a comment
   claiming one.

3. **The opponent adjustment is fit on the opposing DEFENSIVE FRONT's own
   production.** Never on a prior rating of the line being adjusted, and never
   on another line's. See `opponent_strengths` for the constant-valued bug
   that shipped on `defense-vs-position` when a unit was adjusted by its own
   leave-one-out mean of the quantity being rated.

4. **`lineup_hash` is decided by snaps, never by the depth chart.** The spec is
   explicit — "or continuity becomes a description of the team's press
   releases". `depth_charts` supplies the *label* (which of the five slots a
   player occupies); `snap_counts` supplies *who actually played*, and only the
   second decides membership. Within one pass the label mapping is frozen per
   week, so re-publishing a depth chart cannot retroactively move a historical
   hash inside the same capture.

5. **The lineup guard's arithmetic.** When the five change, the window's
   grades were earned by the old five. `apply_lineup_adjustment` moves
   `pressure_rate_allowed_adj` by the departing starters' measured (or
   league-prior) replacement deltas, and `guard.py` asserts at publish time
   that it actually happened. A changed hash with an unmoved adjusted rate is
   the spec's named failure mode and must not reach the lake.
"""

import hashlib
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "ADJUSTMENT_METHOD",
    "LINE_YARDS_CAP",
    "LINE_YARDS_FULL_MAX",
    "LINE_YARDS_HALF_CREDIT",
    "LINE_YARDS_HALF_MAX",
    "LINE_YARDS_LOSS_CREDIT",
    "MIN_DELTA_GAMES",
    "MIN_OPPONENT_STRENGTH",
    "PRECISION",
    "PROVENANCE_MEASURED",
    "PROVENANCE_PRIOR",
    "PROVENANCE_UNAVAILABLE",
    "RECORD_STARTER",
    "RECORD_UNIT",
    "STARTER_POSITIONS",
    "UNCORRECTABLE_LINEUP_CHANGE",
    "UNDETERMINABLE_CHANGE_REASON",
    "DefenseGameTotals",
    "LineTotals",
    "OffenseGameTotals",
    "ReplacementDelta",
    "StarterSlot",
    "apply_lineup_adjustment",
    "build_rows",
    "continuity_games",
    "line_yards",
    "lineup_hash",
    "opponent_strengths",
    "replacement_deltas",
]

# --------------------------------------------------------------------------
# Definitions that must not drift from `defensive-front`
# --------------------------------------------------------------------------

# **The Football Outsiders line-yards weighting.** `defensive-front`'s module
# docstring says outright that "`offensive-line` must import the identical
# weighting"; it cannot literally import it — two services never depend on each
# other — so the values are restated here and
# `tests/test_scale_agreement.py::test_line_yards_weighting_matches_defensive_front`
# reads the sibling module by AST and compares, then compares the two
# `line_yards` curves numerically across the whole plausible yardage range.
# A comment claiming agreement is what the spec's warning is about.
LINE_YARDS_LOSS_CREDIT = 1.2
LINE_YARDS_FULL_MAX = 4.0
LINE_YARDS_HALF_MAX = 10.0
LINE_YARDS_HALF_CREDIT = 0.5
# Derived, never written down: a change to the weighting above cannot leave
# the cap behind.
LINE_YARDS_CAP = LINE_YARDS_FULL_MAX + LINE_YARDS_HALF_CREDIT * (
    LINE_YARDS_HALF_MAX - LINE_YARDS_FULL_MAX
)

# Below this, the opponent-strength index is not trusted to divide by: a slate
# of fronts that generated essentially nothing would otherwise turn a small
# raw rate into an enormous adjusted one. Below the floor the raw figure is
# published unchanged. Same value as `defensive-front`, for the same reason.
MIN_OPPONENT_STRENGTH = 0.05

# Four places is past any meaningful precision in a per-snap rate and keeps the
# lake object from carrying seventeen digits of float noise that differ between
# runs for no reason. Same value as `defensive-front` — a differencing consumer
# rounding the two sides differently would see phantom hundredths.
PRECISION = 4

# `adjustment_method`. It names what this module actually does rather than a
# model it does not implement: leave-one-out, fit on the opposing **defensive
# front's** own production, applied multiplicatively against the league mean.
# The mirror of `defensive-front`'s `opposing_offense_production_ratio_loo_v1`
# — same arithmetic, opposite side of the ball, which is why the two adjusted
# columns are on one scale and may be subtracted.
ADJUSTMENT_METHOD = "opposing_defense_production_ratio_loo_v1"

# --------------------------------------------------------------------------
# The unit
# --------------------------------------------------------------------------

# In position order, left to right. This tuple **is** the `lineup_hash`'s
# ordering: the hash is over the five canonical ids in this sequence, so two
# lines with the same five players in different slots hash differently, which
# is what makes a mid-season swap visible at all.
STARTER_POSITIONS: tuple[str, ...] = ("LT", "LG", "C", "RG", "RT")
POSITION_INDEX = {position: index for index, position in enumerate(STARTER_POSITIONS)}

RECORD_UNIT = "unit"
RECORD_STARTER = "starter"

# How many games on each side of the with/without split a **measured**
# replacement delta needs. Two, because one game is a single opponent and a
# single script — the delta would then be a difference of two game scripts
# wearing a personnel label. Below this the league positional prior is used
# instead and the row says so.
MIN_DELTA_GAMES = 2

PROVENANCE_MEASURED = "measured"
PROVENANCE_PRIOR = "league_positional_prior"
PROVENANCE_UNAVAILABLE = "unavailable"

# Why a whole team — unit row included — is dropped into `coverage.missing`.
# The one case: the five changed and nothing this pass read can say by how
# much the change moves the rate, so the only publishable row would be a stale
# one. See `guard.py`'s docstring for why this is coverage rather than a raise.
UNCORRECTABLE_LINEUP_CHANGE = "lineup_changed_without_replacement_delta"

# **Why `pressure_rate_allowed_adj` is null on an otherwise healthy row.**
#
# `lineup_changed: false` has two meanings and only one of them is safe. It is
# safe when both this game's five and the prior game's five were identified
# and match. It is a **lie** when either could not be identified: the five may
# have moved, the correction was never computed, and the published adjusted
# rate is the one the departed players earned.
#
# Reproduced against this collector's own fixture: blinding one slot of the
# PRIOR game publishes 0.3537 where the corrected value is 0.2342 — **51%
# high** — with coverage, the row's own `lineup_hash`, its five starter rows
# and its schema validity all identical to a healthy pass. The row's own hash
# is fine; only the prior one was missing, and the prior hash is not on the
# row, so no consumer following the schema's "read it together with
# lineup_hash" advice could tell.
#
# It is reachable from the live documents. On the real 2025 season exactly one
# offensive lineman cannot be crosswalked from `snap_counts` to `gsis_id` —
# Alec Anderson (BUF), blank `pfr_id` in `players.csv` — and he played 74 of
# 74 offensive snaps in week 13 and 75 of 75 in week 18. Two full starts that
# cannot be named, either of which produces exactly this state for Buffalo in
# the following week.
#
# So the field is nulled with this reason rather than published uncorrected.
# Everything the uncertainty does not touch — the raw rates, the observed
# opponent-adjusted rate, `adjusted_line_yards`, `mean_time_to_throw` — still
# publishes, which is why this is a null rather than a dropped team.
UNDETERMINABLE_CHANGE_REASON = (
    "lineup_change_undeterminable; the prior or current game's five could not "
    "be identified, so this pass cannot tell a stable line from a changed one "
    "and will not publish a correction it could not compute. "
    "pressure_rate_allowed_adj_observed is unaffected"
)

# `lineup_hash` length. 16 hex characters is 64 bits — collision-free at 32
# teams x 18 weeks by an enormous margin, and short enough to read in a log
# line. `hashlib`, never `hash()`: Python salts `hash()` on `str` per process,
# so a hash built from it would differ between two pods and between one pod's
# restarts, and every week would look like a personnel change.
LINEUP_HASH_CHARS = 16


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, PRECISION)


def _rate(numerator: float, denominator: float) -> float | None:
    """A rate, or `None` when its denominator is zero.

    `None` rather than `0.0`: "this line conceded no pressure" and "this line
    never dropped back" are different facts, and a generator averaging the
    second as a zero would rate a line that never passed as the best pass-
    protecting line in the league.
    """
    return numerator / denominator if denominator else None


def line_yards(rushing_yards: float) -> float:
    """One carry's line-attributed yardage, on the Football Outsiders scale.

    120% credit charged to the line for a run stopped behind the line, full
    credit through four yards, half credit from five to ten, nothing past ten
    — the yards a back gains in the open field are his, not the line's. Capped
    at `LINE_YARDS_CAP`, which is derived from the bands above.

    Byte-for-byte the same curve as `defensive_front.ratings.line_yards`. That
    is the point: `adjusted_line_yards` here and `adjusted_line_yards_allowed`
    there are differenced with no conversion, so a divergence in this function
    corrupts the differential silently rather than failing.
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


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------


@dataclass
class OffenseGameTotals:
    """What one offensive line did in one game.

    Per **game** rather than per season, because two different consumers need
    that granularity: the replacement delta splits a team's window into games
    the departing starter played and games he did not, and the guard's
    correction is computed from that split.
    """

    pass_block_snaps: int = 0
    pressures_allowed: int = 0
    sacks_allowed: int = 0
    time_to_throw_sum: float = 0.0
    time_to_throw_charted: int = 0
    carries: int = 0
    line_yards: float = 0.0

    def add(self, other: "OffenseGameTotals") -> None:
        self.pass_block_snaps += other.pass_block_snaps
        self.pressures_allowed += other.pressures_allowed
        self.sacks_allowed += other.sacks_allowed
        self.time_to_throw_sum += other.time_to_throw_sum
        self.time_to_throw_charted += other.time_to_throw_charted
        self.carries += other.carries
        self.line_yards += other.line_yards


@dataclass
class DefenseGameTotals:
    """What one defensive front produced in one game — the yardstick.

    Keyed by the **producing** unit, which for every metric here is the
    defence. See `opponent_strengths` for what happens when a caller keys it
    by the conceding unit instead.
    """

    pass_rush_snaps: int = 0
    pressures: int = 0
    sacks: int = 0
    carries_faced: int = 0
    line_yards_allowed: float = 0.0


@dataclass
class StarterSlot:
    """One identified starter, before identity resolution."""

    position: str
    gsis_id: str
    snaps: int = 0


@dataclass
class ReplacementDelta:
    """`replacement_delta_pressure_rate`, plus how it was arrived at.

    The spec's hard part: "for most backups there is no observed sample, so
    the adapter must fall back to a positional prior and **mark the field's
    provenance** rather than emitting a modelled number that looks measured."
    `provenance` and `sample_games` are that mark, on the row, so a consumer
    tells a measurement from a prior without joining anything.
    """

    value: float | None
    provenance: str
    sample_games: int = 0


@dataclass
class LineTotals:
    """Everything `build_rows` needs, already folded and joined."""

    offense_game: dict[tuple[str, str], OffenseGameTotals] = field(default_factory=dict)
    defense_game: dict[tuple[str, str], DefenseGameTotals] = field(default_factory=dict)
    # `(team, game_id) -> the other team`, built from the same plays as the
    # totals, so the adjustment cannot be fit against a different schedule
    # than the rates were computed over.
    opponents: dict[tuple[str, str], str] = field(default_factory=dict)
    weeks: set[int] = field(default_factory=set)
    # `game_id -> week`, for ordering a team's games chronologically. From
    # play-by-play, which is the only feed that carries both.
    game_week: dict[str, int] = field(default_factory=dict)
    # `team -> [game_id]`, oldest first. Filled by `order_games`.
    team_games: dict[str, list[str]] = field(default_factory=dict)
    # `(team, game_id) -> the five identified starters`, snap-decided.
    lineups: dict[tuple[str, str], list[StarterSlot]] = field(default_factory=dict)
    # `(team, game_id) -> the team's total offensive snaps that game`, for
    # `starter_snap_share`. Zero when snap counts were unavailable.
    team_offense_snaps: dict[tuple[str, str], int] = field(default_factory=dict)

    def order_games(self) -> None:
        """Fill `team_games` from `opponents`, oldest first.

        Ordered by `(week, game_id)` rather than by insertion: the plays arrive
        in whatever order the upstream document happens to carry them, and
        `continuity_games` counts *consecutive* games. An ordering bug there
        does not raise — it silently reports a stable line as churning.
        """
        by_team: dict[str, list[str]] = {}
        for (team, game_id), _opponent in self.opponents.items():
            by_team.setdefault(team, []).append(game_id)
        self.team_games = {
            team: sorted(set(games), key=lambda g: (self.game_week.get(g, 0), g))
            for team, games in by_team.items()
        }

    def teams(self) -> set[str]:
        """Every team seen on either side of the ball."""
        found = {team for team, _game in self.opponents}
        return found | set(self.opponents.values())

    def offense_totals(self, team: str) -> OffenseGameTotals:
        """One line's window totals, summed from its per-game rows."""
        total = OffenseGameTotals()
        for (offense, _game), line in self.offense_game.items():
            if offense == team:
                total.add(line)
        return total


# --------------------------------------------------------------------------
# The opponent adjustment
# --------------------------------------------------------------------------


def opponent_strengths(
    production: Mapping[tuple[str, str], float],
) -> dict[tuple[str, str], float]:
    """`(front, game) -> that front's strength, excluding that game`.

    **Fit on the producing unit's own output and nothing else** — never on any
    prior rating of the line being adjusted, and never on another line's
    rating. A rating is itself a function of the units faced, so adjusting a
    line by its opponents' *offensive-line* ratings would feed the quantity
    straight back into its own estimate. Circularity of that kind produces
    output that looks entirely fine.

    **Leave-one-out.** A front's strength, as used to adjust the line it
    played, is computed from that front's OTHER games. Without it the game
    being adjusted appears on both sides: a line that shut out a front would
    be told that front is weak partly *because* it was shut out, and would
    have its own achievement adjusted away. A front with exactly one game has
    no other games, so it falls back to that game.

    **A caller that hands this the CONCEDING unit rather than the producing
    one gets a silent constant, not an error.** The mean of a unit's
    leave-one-out means is exactly its full mean, so the strength becomes
    `own_rate / league_mean` and `rate / strength` collapses to the league mean
    identically, for every team. That shipped once on `defense-vs-position`:
    all 32 rows published the same number while the raw values spanned 4x —
    every field populated, every value plausible, and the column carrying no
    information at all. **The tell was zero variance in the adjusted column**,
    which is why `metrics.adjusted_variance` records it on every pass and
    `tests/test_ratings.py` asserts it is non-zero against a fixture built as
    `f(offence) + g(defence)`.

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
    """The mean strength of the fronts `team` actually faced.

    **Re-keyed onto the opponent**, with no per-metric exception. That single
    rule is the whole defence against the bug in `opponent_strengths`'
    docstring: the exception was precisely the defect.
    """
    samples = [
        strengths[(opponent, game)]
        for (offense, game), opponent in opponents.items()
        if offense == team and (opponent, game) in strengths
    ]
    return statistics.fmean(samples) if samples else 1.0


def _adjust(raw: float | None, strength: float) -> float | None:
    if raw is None or strength < MIN_OPPONENT_STRENGTH:
        return raw
    return raw / strength


# --------------------------------------------------------------------------
# Lineups, continuity, and the replacement delta
# --------------------------------------------------------------------------


def lineup_hash(slots: Sequence[StarterSlot]) -> str | None:
    """A stable hash of the five starting ids **in position order**.

    `None` unless all five slots are filled: the spec says a team with fewer
    than five identified starters is reported in `coverage.missing` rather
    than emitted partially, and a hash over four ids would silently be a
    different — and stable — value that no consumer could tell from a real
    five.

    Position order, not sorted-by-id: two lines with the same five players in
    different slots are different lines, and a set hash would call a
    tackle/guard swap continuity.
    """
    by_position = {slot.position: slot.gsis_id for slot in slots}
    if set(by_position) != set(STARTER_POSITIONS):
        return None
    payload = "|".join(by_position[position] for position in STARTER_POSITIONS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:LINEUP_HASH_CHARS]


def continuity_games(hashes: Sequence[str | None]) -> int:
    """Consecutive prior games carrying the current game's `lineup_hash`.

    `hashes` is oldest-first and its **last** element is the current game.
    Counting backwards from the second-to-last, so a line that changed between
    the last two games scores `0` — which is exactly the invariant the guard
    asserts against `lineup_hash` having changed.

    A `None` (a game where fewer than five starters were identified) stops the
    walk rather than being skipped. Skipping would let a game the collector
    could not read join two unrelated lineups into a continuity streak.
    """
    if not hashes or hashes[-1] is None:
        return 0
    current = hashes[-1]
    count = 0
    for previous in reversed(hashes[:-1]):
        if previous != current:
            break
        count += 1
    return count


def _adjusted_game_rate(
    team: str,
    game: str,
    totals: "LineTotals",
    strengths: Mapping[tuple[str, str], float],
) -> float | None:
    """One game's pressure rate allowed, restated against the front faced.

    **Opponent-adjusted, and that is not optional here.** A with/without split
    over a five-game window compares two different opponent slates, and the
    front term is larger than the personnel term it is trying to isolate.
    Measured on this collector's own fixture before the adjustment was added:
    a line that was demonstrably *worse* in the two weeks its backup played
    reported a delta of the wrong sign, because those two weeks happened to
    fall against the league's two weakest fronts. Every field was populated
    and plausible, and the number said the backup was an upgrade.
    """
    line = totals.offense_game.get((team, game))
    if line is None:
        return None
    raw = _rate(line.pressures_allowed, line.pass_block_snaps)
    if raw is None:
        return None
    opponent = totals.opponents.get((team, game))
    return _adjust(raw, strengths.get((opponent, game), 1.0))


def _split_rate(
    team: str,
    games: Sequence[str],
    totals: "LineTotals",
    strengths: Mapping[tuple[str, str], float],
) -> float | None:
    """The mean adjusted rate over one side of a with/without split.

    A plain mean over games rather than a snap-weighted one: each game is one
    observation of the unit against one front, and weighting by snaps would
    let a single blowout with eighty dropbacks outvote three ordinary games.
    """
    rates = [
        rate
        for game in games
        if (rate := _adjusted_game_rate(team, game, totals, strengths)) is not None
    ]
    return statistics.fmean(rates) if rates else None


def replacement_deltas(
    totals: LineTotals, strengths: Mapping[tuple[str, str], float]
) -> dict[tuple[str, str, str], float]:
    """`(team, position, gsis_id) -> measured pressure-rate delta`.

    The **measured** half only. A player's delta is his team's
    opponent-adjusted `pressure_rate_allowed` over the window games he did
    *not* start at that position, minus the rate over the games he did — so a
    positive number means the line got worse without him, which is the sign
    the field's name implies ("change when this starter is replaced").

    Both sides need `MIN_DELTA_GAMES`; anything thinner is a difference of two
    game scripts wearing a personnel label. Everything that fails that test
    falls through to the league positional prior in `_delta_for`, which is the
    honest answer for the majority of backups — the spec says so outright.
    """
    measured: dict[tuple[str, str, str], float] = {}
    for team, games in totals.team_games.items():
        starters_by_game: dict[str, dict[str, str]] = {}
        for game in games:
            slots = totals.lineups.get((team, game), [])
            starters_by_game[game] = {slot.position: slot.gsis_id for slot in slots}

        candidates: set[tuple[str, str]] = {
            (position, gsis_id)
            for by_position in starters_by_game.values()
            for position, gsis_id in by_position.items()
        }
        for position, gsis_id in candidates:
            with_games = [
                game
                for game in games
                if starters_by_game.get(game, {}).get(position) == gsis_id
            ]
            without_games = [
                game
                for game in games
                if position in starters_by_game.get(game, {})
                and starters_by_game[game][position] != gsis_id
            ]
            if (
                len(with_games) < MIN_DELTA_GAMES
                or len(without_games) < MIN_DELTA_GAMES
            ):
                continue
            with_rate = _split_rate(team, with_games, totals, strengths)
            without_rate = _split_rate(team, without_games, totals, strengths)
            if with_rate is None or without_rate is None:
                continue
            measured[(team, position, gsis_id)] = without_rate - with_rate
    return measured


def _current_starters(totals: LineTotals) -> set[tuple[str, str, str]]:
    """`(team, position, gsis_id)` for the five each team last fielded."""
    found: set[tuple[str, str, str]] = set()
    for team, games in totals.team_games.items():
        if not games:
            continue
        for slot in totals.lineups.get((team, games[-1]), []):
            found.add((team, slot.position, slot.gsis_id))
    return found


def _positional_priors(
    measured: Mapping[tuple[str, str, str], float],
    current: set[tuple[str, str, str]],
) -> dict[str, float]:
    """League mean measured delta at each slot, over CURRENT starters only.

    **An empirical prior, not an invented constant.** Every value is the mean
    of deltas this same pass measured across the league, so a collector with
    no measured delta at a slot publishes `None` and says `unavailable` rather
    than a number nobody can source. That is the honest-nulls rule applied to
    a modelled field: the alternative — a hard-coded 0.02 for a tackle — is
    exactly the "modelled number that looks measured" the spec forbids.

    **Restricted to current starters, which is the whole reason this is not a
    one-liner.** Any window in which a starter was replaced yields two
    measured deltas that are near-exact negatives of each other: the
    incumbent's ("worse without him") and the deputy's ("better without him").
    Averaging both pools them to approximately zero — measured on this
    collector's own fixture, a prior of exactly `0.0` for left tackle — and a
    zero prior is not a weak prior, it is a claim that losing a starting
    tackle costs nothing. The prior is meant to answer "what does losing a
    STARTER cost", so only starters are in it.
    """
    grouped: dict[str, list[float]] = {}
    for key, value in measured.items():
        if key not in current:
            continue
        grouped.setdefault(key[1], []).append(value)
    return {
        position: statistics.fmean(values)
        for position, values in grouped.items()
        if values
    }


def _delta_for(
    team: str,
    position: str,
    gsis_id: str,
    measured: Mapping[tuple[str, str, str], float],
    priors: Mapping[str, float],
    sample_games: Mapping[tuple[str, str, str], int],
) -> ReplacementDelta:
    key = (team, position, gsis_id)
    if key in measured:
        return ReplacementDelta(
            value=_round(measured[key]),
            provenance=PROVENANCE_MEASURED,
            sample_games=sample_games.get(key, 0),
        )
    if position in priors:
        return ReplacementDelta(
            value=_round(priors[position]), provenance=PROVENANCE_PRIOR
        )
    return ReplacementDelta(value=None, provenance=PROVENANCE_UNAVAILABLE)


def apply_lineup_adjustment(
    observed_adj: float | None, delta: float | None
) -> float | None:
    """The spec's publish-time correction, in one place so a test can remove it.

    `observed_adj` is the window's opponent-adjusted pressure rate — computed
    over snaps the *departed* starter took. `delta` is the sum of the departing
    starters' replacement deltas. The corrected value is what a generator
    should project the *upcoming* week from; `pressure_rate_allowed_adj_observed`
    is published beside it so the strictly symmetric counterpart to
    `defensive-front`'s `pressure_rate_generated_adj` is still available.

    Extracted rather than inlined because `guard.py` asserts the correction
    happened, and a guard whose subject cannot be independently disabled is
    a guard no test can prove is doing anything.
    """
    if observed_adj is None or delta is None:
        return observed_adj
    return observed_adj + delta


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def build_rows(
    totals: LineTotals,
    *,
    availability: Mapping[str, str],
    canonical_ids: Mapping[str, str],
    degraded: tuple[str, ...],
    null_field_reason: Mapping[str, str],
) -> tuple[list[dict], dict[str, str]]:
    """Every publishable row, plus the teams dropped wholesale and why.

    Returns `(rows, dropped)` where `dropped` is `team -> reason` for a team
    whose lineup changed and which this pass has no replacement delta to
    correct — the one case where even the unit row must not publish. The
    caller turns that into `acc.fail(...)`; nothing here touches coverage, so
    the accounting stays in one place.

    A team whose five starters are not all identified emits **no** starter
    rows — the spec's own clause, all-or-nothing rather than partial — and
    still emits its unit row, because the unit row is what drives the
    projection and its rates do not depend on knowing anybody's name.
    """
    teams = sorted(totals.teams())
    window_weeks = len(totals.weeks)

    pressure_production = {
        key: rate
        for key, line in totals.defense_game.items()
        if (rate := _rate(line.pressures, line.pass_rush_snaps)) is not None
    }
    sack_production = {
        key: rate
        for key, line in totals.defense_game.items()
        if (rate := _rate(line.sacks, line.pass_rush_snaps)) is not None
    }
    line_yard_production = {
        key: rate
        for key, line in totals.defense_game.items()
        if (rate := _rate(line.line_yards_allowed, line.carries_faced)) is not None
    }
    pressure_strength = opponent_strengths(pressure_production)
    sack_strength = opponent_strengths(sack_production)
    line_yard_strength = opponent_strengths(line_yard_production)

    measured = replacement_deltas(totals, pressure_strength)
    sample_games = _measured_sample_sizes(totals)
    priors = _positional_priors(measured, _current_starters(totals))

    rows: list[dict] = []
    dropped: dict[str, str] = {}

    for team in teams:
        line = totals.offense_totals(team)
        faced_pressure = _faced_strength(team, pressure_strength, totals.opponents)
        faced_sack = _faced_strength(team, sack_strength, totals.opponents)
        faced_line_yards = _faced_strength(team, line_yard_strength, totals.opponents)

        pressure_rate = _rate(line.pressures_allowed, line.pass_block_snaps)
        sack_rate = _rate(line.sacks_allowed, line.pass_block_snaps)
        raw_line_yards = _rate(line.line_yards, line.carries)

        observed_adj = _round(_adjust(pressure_rate, faced_pressure))

        games = totals.team_games.get(team, [])
        hashes = [lineup_hash(totals.lineups.get((team, game), [])) for game in games]
        current_hash = hashes[-1] if hashes else None
        prior_hash = hashes[-2] if len(hashes) >= 2 else None
        has_prior = len(hashes) >= 2
        # **Three states, not two.** With no prior game nothing could have
        # changed, so the status is known. With a prior game it is known only
        # when both fives were identified. See `UNDETERMINABLE_CHANGE_REASON`.
        change_known = not has_prior or (
            current_hash is not None and prior_hash is not None
        )
        changed = change_known and has_prior and current_hash != prior_hash
        streak = continuity_games(hashes)

        if change_known:
            adjustment, adjustment_ok = _lineup_correction(
                team,
                games,
                totals,
                changed=changed,
                measured=measured,
                priors=priors,
                sample_games=sample_games,
            )
            corrected_adj = _round(apply_lineup_adjustment(observed_adj, adjustment))
            reasons = dict(null_field_reason)
        else:
            # Not a correction of zero — no correction. A zero would claim the
            # five are unchanged, which is exactly what this pass cannot say.
            adjustment, adjustment_ok = None, True
            corrected_adj = None
            reasons = {
                **null_field_reason,
                "pressure_rate_allowed_adj": UNDETERMINABLE_CHANGE_REASON,
                "lineup_adjustment_pressure_rate": UNDETERMINABLE_CHANGE_REASON,
            }

        unit_row = {
            "team_id": team,
            "record_type": RECORD_UNIT,
            "pass_block_snaps": line.pass_block_snaps,
            "run_block_snaps": line.carries,
            "pressure_rate_allowed": _round(pressure_rate),
            "pressure_rate_allowed_adj": corrected_adj,
            "pressure_rate_allowed_adj_observed": observed_adj,
            "sack_rate_allowed": _round(sack_rate),
            "sack_rate_allowed_adj": _round(_adjust(sack_rate, faced_sack)),
            "mean_time_to_throw": _round(
                _rate(line.time_to_throw_sum, line.time_to_throw_charted)
            ),
            "adjusted_line_yards": _round(_adjust(raw_line_yards, faced_line_yards)),
            # Null by necessity, with the reason on the row, and symmetric with
            # `defensive-front`'s two fields of the same stem. See
            # `null_field_reason` and this collector's README. **Neither
            # appears in the coverage predicate** — an unsourceable field in
            # the predicate pins the ratio at zero forever and destroys its
            # ability to report a truncated upstream.
            "yards_before_contact_per_carry": None,
            "yards_before_contact_per_carry_adj": None,
            "lineup_hash": current_hash,
            "continuity_games": streak,
            "lineup_changed": changed,
            # The third state, on the row. `lineup_changed: false` alone
            # cannot distinguish "verified stable" from "could not tell", and
            # the row's own `lineup_hash` does not help — in the deceptive
            # case it is the PRIOR game's hash that is missing.
            "lineup_change_known": change_known,
            "lineup_adjustment_pressure_rate": adjustment,
            "opponent_pressure_strength_index": _round(faced_pressure),
            "adjustment_method": ADJUSTMENT_METHOD,
            "adjustment_window_weeks": window_weeks,
            "degraded_upstreams": list(degraded),
            "null_field_reason": reasons,
        }

        if changed and not adjustment_ok:
            # The line changed and this pass has no delta to correct it with.
            # Publishing the row anyway is the spec's named failure mode — an
            # elite grade carried into the exact week it will be worst, with
            # nothing about the row looking malformed. So the whole team is
            # owed and unfilled, rather than emitted stale.
            dropped[team] = UNCORRECTABLE_LINEUP_CHANGE
            continue

        rows.append(unit_row)

        slots = totals.lineups.get((team, games[-1]), []) if games else []
        rows.extend(
            _starter_rows(
                team,
                slots,
                current_hash,
                totals=totals,
                games=games,
                availability=availability,
                canonical_ids=canonical_ids,
                measured=measured,
                priors=priors,
                sample_games=sample_games,
            )
        )

    return rows, dropped


def _measured_sample_sizes(totals: LineTotals) -> dict[tuple[str, str, str], int]:
    """How many games each measured delta's *with* side was built from."""
    sizes: dict[tuple[str, str, str], int] = {}
    for team, games in totals.team_games.items():
        for game in games:
            for slot in totals.lineups.get((team, game), []):
                key = (team, slot.position, slot.gsis_id)
                sizes[key] = sizes.get(key, 0) + 1
    return sizes


def _lineup_correction(
    team: str,
    games: Sequence[str],
    totals: LineTotals,
    *,
    changed: bool,
    measured: Mapping[tuple[str, str, str], float],
    priors: Mapping[str, float],
    sample_games: Mapping[tuple[str, str, str], int],
) -> tuple[float | None, bool]:
    """The delta to add to `pressure_rate_allowed_adj`, and whether it is usable.

    Summed over the positions whose starter changed between the prior game and
    the current one, taking the **departing** starter's delta: the field means
    "change in `pressure_rate_allowed` when this starter is replaced", and the
    starter who was replaced is the one who left.

    `(0.0, True)` when nothing changed — a no-op correction is still a
    correction that ran, and the guard only requires a *non-zero* one on a
    changed lineup.

    `(None, False)` when a changed position has no delta at all, measured or
    prior. The caller drops the team into `coverage.missing`: an uncorrectable
    changed lineup is precisely the stale unit the spec says must not publish.
    """
    if not changed or len(games) < 2:
        return (0.0, True)
    current = {
        slot.position: slot.gsis_id
        for slot in totals.lineups.get((team, games[-1]), [])
    }
    previous = {
        slot.position: slot.gsis_id
        for slot in totals.lineups.get((team, games[-2]), [])
    }
    total = 0.0
    for position in STARTER_POSITIONS:
        outgoing = previous.get(position)
        if outgoing is None or outgoing == current.get(position):
            continue
        delta = _delta_for(team, position, outgoing, measured, priors, sample_games)
        if delta.value is None:
            return (None, False)
        total += delta.value
    return (_round(total), True)


def _starter_rows(
    team: str,
    slots: Sequence[StarterSlot],
    current_hash: str | None,
    *,
    totals: LineTotals,
    games: Sequence[str],
    availability: Mapping[str, str],
    canonical_ids: Mapping[str, str],
    measured: Mapping[tuple[str, str, str], float],
    priors: Mapping[str, float],
    sample_games: Mapping[tuple[str, str, str], int],
) -> list[dict]:
    """The five starter rows, or none at all.

    All-or-nothing on purpose: the spec's coverage clause says a team with
    fewer than five identified starters is reported in `coverage.missing`
    rather than emitted partially. A partial five also silently changes what
    `lineup_hash` means, since the hash is over exactly five ids in order.

    The caller derives the missing keys from the rows that came back, so
    "which slots are owed" is computed once rather than in two places that
    could disagree.
    """
    by_position = {slot.position: slot for slot in slots}
    if set(by_position) != set(STARTER_POSITIONS):
        return []

    resolved = {
        position: canonical_ids.get(slot.gsis_id)
        for position, slot in by_position.items()
    }
    if any(value is None for value in resolved.values()):
        # An id `player-identity` refused is a missing row with a reason,
        # never a raw GSIS id adopted as canonical: a generator joining on one
        # matches nothing, and a total join failure wearing a complete-looking
        # field is worse than an honest hole.
        return []

    window_snaps = sum(totals.team_offense_snaps.get((team, game), 0) for game in games)
    rows: list[dict] = []
    for position in STARTER_POSITIONS:
        slot = by_position[position]
        played = sum(
            next(
                (
                    other.snaps
                    for other in totals.lineups.get((team, game), [])
                    if other.gsis_id == slot.gsis_id
                ),
                0,
            )
            for game in games
        )
        delta = _delta_for(team, position, slot.gsis_id, measured, priors, sample_games)
        rows.append(
            {
                "team_id": team,
                "record_type": RECORD_STARTER,
                "lineup_hash": current_hash,
                "starter_id": resolved[position],
                "starter_position": position,
                "starter_snap_share": _round(_rate(played, window_snaps)),
                "starter_availability": availability.get(slot.gsis_id, "active"),
                "replacement_delta_pressure_rate": delta.value,
                "replacement_delta_provenance": delta.provenance,
                "replacement_delta_sample_games": delta.sample_games,
            }
        )
    return rows

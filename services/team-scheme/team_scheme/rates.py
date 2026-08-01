"""Team-season rates, and the guard that keeps the window a team-season.

--------------------------------------------------------------------------
Why the window is a team-season, and why that is the safe answer
--------------------------------------------------------------------------

The original `coaching-scheme` spec keyed these rates to a **staff revision**,
so that a mid-season coordinator change produced two profiles rather than one
blend. That is the better product, and it is unavailable: no reliable free
source exists for per-week NFL coaching staff, and the one that looks like it
does (nfldata's `games.csv` coach columns) is *correct through 2023 and wrong
from 2024*, carrying the opening-day coach on every row. Keying a rate to a
revision timeline derived from that feed does not leave a gap — it makes a
**confident false attribution**, claiming one regime where there were two, and
publishing it as a fact about a named coach. See
`docs/architecture/phase-8-data-source-collectors.md`, `#### coaching-staff`.

So the rates key to a `(team, season)`. A mid-season change now produces an
honest *blend*: a true statement about the season and a poor predictor of next
week, which the README says out loud. Consumers wanting regime-aware rates
need `coaching-staff` to exist first.

**Do not reintroduce revision-keyed rates against an unreliable staff feed.**
That converts an honest blend into a confident false attribution, and the
number is indistinguishable from a correct one downstream.

--------------------------------------------------------------------------
Structure first, assertion second
--------------------------------------------------------------------------

`assert_window_is_the_team_season` is the *second* line of defence. The first
is that a wrong window is nearly unrepresentable: `pbp.py` accumulates only
per `(team, week)`, and `aggregate` folds the buckets belonging to one team.
There is no season-to-date accumulator keyed to anything else, and no
incremental state to invalidate.

That is deliberate design, not luck. The wrong window re-appears the moment
somebody folds by week range, by opponent, or by a revision id restored from
`coaching-staff` — and the assertion is what makes that a test failure rather
than a plausible-looking number.

--------------------------------------------------------------------------
The three arms, and why they are three
--------------------------------------------------------------------------

`assert_window_is_the_team_season` refuses on any of:

1. **A foreign team's bucket in the fold.** The direct form: this row claims
   to describe one team and part of its sample came from another. This is the
   arm that enforces the removed coupling's replacement — a fold keyed to
   anything wider than the team dies here.
2. **A sampled week outside the regular season.** A postseason or
   week-0 row leaking past the adapter's `season_type` filter would extend the
   window past the grid the season is defined on.
3. **`games_sampled` exceeding the number of weeks sampled.** The indirect
   form, and the one arms 1 and 2 cannot see. A team plays at most one game a
   week, so twelve weeks cannot hold thirteen games. This catches a bucket
   that accumulated two games under one `(team, week)` key — every week
   correctly attributed to the right team and inside the season, and the
   sample the rates are computed over silently larger than the window.
   Arms 1 and 2 pass it happily.

   The ceiling is `sampled_weeks`, not a wider "every week folded" count. A
   wider one was tried first, on the theory that a bye-week bucket is folded
   without contributing a snap — but `bucket.games` is only added to inside
   the offensive-snap branch, so a snapless week contributes no game either
   and the two ceilings cannot be told apart by any input the adapter can
   produce. Mutating one into the other survived the whole suite, which is
   what an unfalsifiable distinction looks like. The tighter one is kept, and
   it is also the one already published on the row, so a consumer can check
   the arm rather than trust it.

They fail the same way from the outside — a refused row and a coverage entry —
and are therefore easy to mistake for one check. They have separate fixtures
for that reason.

A refusal drops the row and records the team as missing with a reason.
Refusing is right, and the choice is not close: a rate folded over the wrong
buckets is not a degraded measurement, it is a measurement of a thing that
does not exist, and it is indistinguishable from a real one downstream.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .adapters.ftn import ChartingBucket
from .adapters.participation import GROUPING_KEYS, PersonnelBucket
from .adapters.pbp import WeeklyBucket

__all__ = [
    "RATE_PRECISION",
    "REASON_FOREIGN_TEAM_IN_WINDOW",
    "REASON_GAMES_EXCEED_WEEKS",
    "REASON_WEEK_OUTSIDE_SEASON",
    "REGULAR_SEASON_MAX_WEEK",
    "TeamSeasonRates",
    "WindowIsNotTheTeamSeason",
    "aggregate",
    "assert_window_is_the_team_season",
    "weeks_in_team_season",
]

REASON_FOREIGN_TEAM_IN_WINDOW = "rate_window_includes_another_team"
REASON_WEEK_OUTSIDE_SEASON = "rate_window_includes_a_week_outside_the_season"
REASON_GAMES_EXCEED_WEEKS = "games_sampled_exceeds_weeks_folded"

# The NFL regular season has run to 18 weeks since 2021 and to 17 before that.
# A ceiling rather than an exact length: a 17-week season simply never produces
# a week 18, while a postseason row that slipped past the adapter's
# `season_type` filter produces a 19+ and is refused. Naming it keeps the arm
# from reading as a magic number that could be "tidied" to 17.
REGULAR_SEASON_MAX_WEEK = 18

# Rates are reported to four decimal places.
#
# **An earlier revision of this comment, in `coaching-scheme`, claimed rounding
# was what kept the digest gate stable against floating-point summation order.
# That was wrong**, and the correction is carried across rather than dropped:
# 300,000 trials over PROE-magnitude signed values (-35..20) at 50, 120 and 300
# terms produced **zero** sums whose value depended on addition order, and the
# same for clock-magnitude positives. At these magnitudes and counts the
# instability does not arise, and `aggregate` folds in sorted-week order
# anyway, so the arithmetic is deterministic without any help from rounding.
#
# What rounding actually does is bound the *published* precision, which is a
# contract property rather than a stability one: consumers get four decimals,
# and `test_a_published_rate_is_bounded_to_four_decimals` pins it. Digest
# stability comes from the determinism of the fold, which the digest-gate
# tests exercise directly.
RATE_PRECISION = 4


class WindowIsNotTheTeamSeason(ValueError):
    """A rate window that is not exactly one team's regular season.

    Carries `reason` so the caller can record *which* arm refused without
    re-deriving it from the message. Subclasses `ValueError`, so if one ever
    escapes to `CollectorMetrics.reason_for` it classifies as `malformed`,
    which is what it is: the collector built a row that cannot be true.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TeamSeasonRates:
    """One team-season's scheme profile. Every rate is null when unsourced.

    A null here is always "no evidence", never "zero". `no_huddle_rate: 0.0`
    means a team that huddled on every snap; `None` means we could not
    measure. Collapsing the two would make a missing charting feed read as a
    team that never used motion.

    `folded_teams` is **provenance for the guard**, not product: it names the
    teams whose buckets actually entered this fold, read from the buckets
    themselves rather than from `aggregate`'s `team_id` argument. That
    distinction is the whole of arm 1 — derive it from the argument and the
    arm becomes a tautology that can never fire, which is exactly the mutation
    `test_a_bucket_keyed_to_one_team_but_carrying_another_is_refused` exists
    to kill. It is deliberately not published: a consumer verifies the window
    from `sampled_weeks`, which is on the row.
    """

    games_sampled: int
    sampled_weeks: tuple[int, ...]
    folded_teams: frozenset[str]
    neutral_pass_rate: float | None
    pass_rate_over_expected: float | None
    sec_per_play_neutral: float | None
    no_huddle_rate: float | None
    shotgun_rate: float | None
    play_action_rate: float | None
    pre_snap_motion_rate: float | None
    personnel_rates: dict[str, float] | None
    fourth_down_go_rate: float | None


def weeks_in_team_season(
    team_id: str, play_buckets: dict[tuple[str, int], WeeklyBucket]
) -> tuple[int, ...]:
    """Every week this team has a bucket for, ascending.

    The single place a team-season is turned into a set of weeks. Every rate is
    computed over the output of this function and nothing else, which is what
    makes the guard a check on one boundary case rather than on arithmetic
    scattered across the module.

    Ascending, and that is not cosmetic: `aggregate` folds in this order and
    `sampled_weeks` is published from it, so an unsorted return would make the
    digest depend on dict iteration order and defeat the unchanged-snapshot
    gate in `capture.py`.
    """
    return tuple(sorted(week for (team, week) in play_buckets if team == team_id))


def assert_window_is_the_team_season(team_id: str, rates: TeamSeasonRates) -> None:
    """The guard. Raises `WindowIsNotTheTeamSeason`; returns `None` when clean.

    An empty window is clean: a team with no games yet has nothing to blend,
    and refusing it would turn "this team has not played" into an error. It is
    recorded as missing by `capture` for that reason instead, which is a
    different and more accurate statement.
    """
    foreign = sorted(rates.folded_teams - {team_id})
    if foreign:
        raise WindowIsNotTheTeamSeason(
            REASON_FOREIGN_TEAM_IN_WINDOW,
            f"{team_id}'s profile folded buckets belonging to {foreign}",
        )

    outside = [
        week for week in rates.sampled_weeks if not 1 <= week <= REGULAR_SEASON_MAX_WEEK
    ]
    if outside:
        raise WindowIsNotTheTeamSeason(
            REASON_WEEK_OUTSIDE_SEASON,
            f"{team_id}'s sample includes week(s) {sorted(outside)}, outside "
            f"1-{REGULAR_SEASON_MAX_WEEK}",
        )

    weeks = len(rates.sampled_weeks)
    if rates.games_sampled > weeks:
        raise WindowIsNotTheTeamSeason(
            REASON_GAMES_EXCEED_WEEKS,
            f"{team_id} sampled {weeks} week(s) but {rates.games_sampled} "
            "game(s) were counted; a team plays at most one game per week",
        )


def _rate(numerator: float, denominator: float) -> float | None:
    """A share, or `None` when nothing was observed.

    `None` rather than 0.0 on an empty denominator. A team with no neutral
    snaps has no neutral pass rate; reporting 0.0 would say it ran on every
    one of them.
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator, RATE_PRECISION)


def aggregate(
    team_id: str,
    weeks: Sequence[int],
    *,
    play_buckets: dict[tuple[str, int], WeeklyBucket],
    charting_buckets: dict[tuple[str, int], ChartingBucket] | None,
    personnel_buckets: dict[tuple[str, int], PersonnelBucket] | None,
) -> TeamSeasonRates:
    """Fold the selected weeks into one team-season's rates.

    **Counts are summed and divided once**, never averaged across weeks. A
    mean of per-week rates would weight a 40-snap blowout equally with a
    75-snap game, and the blowout is exactly the week whose play-calling is
    least representative.

    `charting_buckets`/`personnel_buckets` are `None` when their feed was
    unavailable this pass, and the fields they own come back `None` — the
    degraded path, not a failure.
    """
    selected = [
        play_buckets[(team_id, week)]
        for week in weeks
        if (team_id, week) in play_buckets
    ]

    games: set[str] = set()
    offensive = shotgun = no_huddle = 0
    neutral = neutral_pass = neutral_clock_samples = 0
    neutral_seconds = 0.0
    proe_sum = 0.0
    proe_plays = 0
    fourth_decisions = fourth_gos = 0
    for bucket in selected:
        games |= bucket.games
        offensive += bucket.offensive_plays
        shotgun += bucket.shotgun_plays
        no_huddle += bucket.no_huddle_plays
        neutral += bucket.neutral_plays
        neutral_pass += bucket.neutral_passes
        neutral_seconds += bucket.neutral_clock_seconds
        neutral_clock_samples += bucket.neutral_clock_samples
        proe_sum += bucket.proe_sum
        proe_plays += bucket.proe_plays
        fourth_decisions += bucket.fourth_down_decisions
        fourth_gos += bucket.fourth_down_gos

    play_action = motion = charted = 0
    if charting_buckets is not None:
        for week in weeks:
            charting = charting_buckets.get((team_id, week))
            if charting is None:
                continue
            charted += charting.charted_plays
            motion += charting.motion_plays
            play_action += charting.play_action_plays

    personnel: dict[str, float] | None = None
    if personnel_buckets is not None:
        classified = 0
        totals = dict.fromkeys(GROUPING_KEYS, 0)
        for week in weeks:
            grouping = personnel_buckets.get((team_id, week))
            if grouping is None:
                continue
            classified += grouping.classified_plays
            for name in GROUPING_KEYS:
                totals[name] += grouping.groupings[name]
        if classified > 0:
            personnel = {
                name: round(totals[name] / classified, RATE_PRECISION)
                for name in GROUPING_KEYS
            }

    # `pass_oe` is already in percentage points, so this is a mean rather than
    # a share and does not go through `_rate`.
    proe = round(proe_sum / proe_plays, RATE_PRECISION) if proe_plays > 0 else None

    return TeamSeasonRates(
        games_sampled=len(games),
        # `offensive_plays > 0` narrows this to the weeks that actually
        # contributed. A bye or an unplayed week inside the range is not part
        # of the rate window, and widening what feeds this weakens the guard's
        # season-bounds arm silently.
        sampled_weeks=tuple(
            sorted(bucket.week for bucket in selected if bucket.offensive_plays > 0)
        ),
        # Provenance for the guard, read from the buckets themselves rather
        # than from the `team_id` argument — a selection that reached past its
        # argument, or a bucket filed under the wrong key, would otherwise be
        # invisible to arm 1.
        folded_teams=frozenset(bucket.team_id for bucket in selected),
        neutral_pass_rate=_rate(neutral_pass, neutral),
        pass_rate_over_expected=proe,
        sec_per_play_neutral=_rate(neutral_seconds, neutral_clock_samples),
        no_huddle_rate=_rate(no_huddle, offensive),
        shotgun_rate=_rate(shotgun, offensive),
        play_action_rate=(
            _rate(play_action, charted) if charting_buckets is not None else None
        ),
        pre_snap_motion_rate=(
            _rate(motion, charted) if charting_buckets is not None else None
        ),
        personnel_rates=personnel,
        fourth_down_go_rate=_rate(fourth_gos, fourth_decisions),
    )

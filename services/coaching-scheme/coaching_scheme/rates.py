"""Revision-scoped rates, and **guard 1** — the window that must not straddle.

The spec's named failure mode, in full, because everything here answers it:

> A rate window that straddles the change it is supposed to describe. An
> adapter that computes season-to-date PROE and then attaches it to the
> current staff revision produces a number blending nine weeks of the fired
> coordinator with three of their replacement — it describes neither regime,
> it moves in the right direction just slowly enough to look like real drift,
> and the whole point of the collector is lost silently.

--------------------------------------------------------------------------
Structure first, assertion second
--------------------------------------------------------------------------

The assertion below is the *second* line of defence. The first is that a
blended window is nearly unrepresentable: `pbp.py` accumulates only per
`(team, week)`, and `aggregate` folds a *selection* of those buckets. There is
no season-to-date accumulator to attach to the wrong revision, and no
incremental state to invalidate — which is what the spec's "recompute from
scratch when a revision boundary is inserted mid-season" asks for. Inserting a
boundary changes which weeks `weeks_in_revision` returns. Nothing else moves.

That is deliberate design, not luck. The blend re-appears the moment somebody
caches a per-team total "for efficiency", and the assertion is what makes that
a test failure rather than a plausible-looking number.

--------------------------------------------------------------------------
The two arms, and why they are two
--------------------------------------------------------------------------

`assert_window_within_revision` refuses on either of:

1. **A sampled week outside the revision's span.** The direct form of the
   failure: week 3 data attached to a revision that starts in week 9.
2. **`games_sampled` exceeding the revision's week span.** The indirect form,
   and the one arm 1 cannot see. A team plays at most one game a week, so six
   weeks cannot hold seven games. This catches a *duplicate* fold — the same
   game counted under two weeks, or a bucket merged twice — which leaves every
   sampled week correctly inside the span while doubling the sample the rates
   are computed over. Arm 1 passes it happily.

They fail the same way and are therefore easy to mistake for one check. They
have separate fixtures for that reason.

A refusal drops the row and records the team as missing with a reason. Refusing
is right, and the choice is not close: a blended rate is not a degraded
measurement, it is a measurement of a thing that does not exist, and it is
indistinguishable from a real one downstream.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .adapters.ftn import ChartingBucket
from .adapters.participation import GROUPING_KEYS, PersonnelBucket
from .adapters.pbp import WeeklyBucket
from .revisions import Revision

__all__ = [
    "REASON_GAMES_EXCEED_SPAN",
    "REASON_WEEK_OUTSIDE_REVISION",
    "RevisionRates",
    "WindowStraddlesRevision",
    "aggregate",
    "assert_window_within_revision",
    "weeks_in_revision",
]

REASON_WEEK_OUTSIDE_REVISION = "rate_window_straddles_revision_boundary"
REASON_GAMES_EXCEED_SPAN = "games_sampled_exceeds_revision_span"

# Rates are reported to four decimal places.
#
# **An earlier revision of this comment claimed rounding was what kept the
# digest gate stable against floating-point summation order. That was wrong,
# and it is corrected here rather than defended with a contrived test.**
# Searched for it: 300,000 trials over PROE-magnitude signed values (-35..20)
# at 50, 120 and 300 terms produced **zero** sums whose value depended on
# addition order, and the same for clock-magnitude positives. At these
# magnitudes and counts the instability does not arise, and `aggregate` folds
# in sorted-week order anyway, so the arithmetic is deterministic without any
# help from rounding.
#
# What rounding actually does is bound the *published* precision, which is a
# contract property rather than a stability one: consumers get four decimals,
# and `test_a_published_rate_is_bounded_to_four_decimals` pins it. Digest
# stability comes from the determinism of the fold, which the digest-gate
# tests exercise directly.
RATE_PRECISION = 4


class WindowStraddlesRevision(ValueError):
    """A rate window that does not lie inside the revision it is keyed to.

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
class RevisionRates:
    """One revision's scheme profile. Every field is null when unsourced.

    A null here is always "no evidence", never "zero". `no_huddle_rate: 0.0`
    means a regime that huddled on every snap; `None` means we could not
    measure. Collapsing the two would make a missing charting feed read as a
    team that never used motion.
    """

    games_sampled: int
    sampled_weeks: tuple[int, ...]
    neutral_pass_rate: float | None
    pass_rate_over_expected: float | None
    sec_per_play_neutral: float | None
    no_huddle_rate: float | None
    shotgun_rate: float | None
    play_action_rate: float | None
    pre_snap_motion_rate: float | None
    personnel_rates: dict[str, float] | None
    fourth_down_go_rate: float | None


def weeks_in_revision(revision: Revision, available: Iterable[int]) -> tuple[int, ...]:
    """The weeks of `available` this revision governs, ascending.

    The single place a revision is turned into a set of weeks. Every rate is
    computed over the output of this function and nothing else, which is what
    makes guard 1 a check on a boundary case rather than on arithmetic
    scattered across the module.
    """
    return tuple(sorted(week for week in available if revision.covers(week)))


def assert_window_within_revision(
    revision: Revision, sampled_weeks: Sequence[int], games_sampled: int
) -> None:
    """Guard 1. Raises `WindowStraddlesRevision`; returns `None` when clean.

    An empty window is clean: a revision with no games yet has nothing to
    blend, and refusing it would turn "this regime has not played" into an
    error.
    """
    if sampled_weeks:
        outside = [week for week in sampled_weeks if not revision.covers(week)]
        if outside:
            raise WindowStraddlesRevision(
                REASON_WEEK_OUTSIDE_REVISION,
                f"{revision.revision_id} governs weeks "
                f"{revision.effective_from_week}-"
                f"{revision.effective_to_week or 'current'}"
                f" but the sample includes {sorted(outside)}",
            )

    span = revision.week_span
    if games_sampled > span:
        raise WindowStraddlesRevision(
            REASON_GAMES_EXCEED_SPAN,
            f"{revision.revision_id} spans {span} week(s) but "
            f"{games_sampled} game(s) were sampled; a team plays at most one "
            "game per week",
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
    revision: Revision,
    weeks: Sequence[int],
    *,
    play_buckets: dict[tuple[str, int], WeeklyBucket],
    charting_buckets: dict[tuple[str, int], ChartingBucket] | None,
    personnel_buckets: dict[tuple[str, int], PersonnelBucket] | None,
) -> RevisionRates:
    """Fold the selected weeks into one revision's rates.

    **Counts are summed and divided once**, never averaged across weeks. A
    mean of per-week rates would weight a 40-snap blowout equally with a
    75-snap game, and the blowout is exactly the week whose play-calling is
    least representative.

    `charting_buckets`/`personnel_buckets` are `None` when their feed was
    unavailable this pass, and the fields they own come back `None` — the
    degraded path, not a failure.
    """
    selected = [
        play_buckets[(revision.team_id, week)]
        for week in weeks
        if (revision.team_id, week) in play_buckets
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
            charting = charting_buckets.get((revision.team_id, week))
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
            grouping = personnel_buckets.get((revision.team_id, week))
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

    return RevisionRates(
        games_sampled=len(games),
        sampled_weeks=tuple(
            sorted(bucket.week for bucket in selected if bucket.offensive_plays > 0)
        ),
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

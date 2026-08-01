"""Derived aggregates: rates, medians, trajectories, and when to refuse them.

`events.py` reconstructs what happened. This module turns it into the numbers
the generator asks for — and, just as importantly, decides when a number cannot
be defended and must be `None` instead.

**Three refusals, and every one of them is the point of this file.**

1. **Below the sample floor, the derived rates are null.** The spec: "aggregates
   with `sample_size_events` below the configured floor are emitted with the raw
   events but with the derived rates null." A soft-tissue recurrence rate of
   0.50 off two events is not a rate, it is a coin flip wearing a decimal point,
   and a projection weighting it cannot tell the difference. The raw events are
   still published, so a consumer with its own opinion about small samples can
   compute whatever it likes.

2. **`availability_rate` with zero games possible is null, never 1.0.** A player
   the window observed no completed games for has no availability to report, and
   `1 - 0/0` reading as a perfect record is the same class of error as
   `Coverage.ratio` returning 1.0 for `expected: 0`.

3. **`age_adjusted_availability_rate` below the cohort floor is null.** Ranking a
   28-year-old tight end against the two other 28-year-old tight ends this pass
   captured produces a confident-looking ratio with no population behind it.

**The reference populations are the pass's own captured players**, not all of
history, for the same reason `player-profile`'s percentiles are: the alternative
is a table this collector would have to maintain and version, and a consumer can
reproduce a population it can see. `GET /signals/return-profile` republishes the
return distribution for exactly that reason.
"""

import os
import statistics
from datetime import date

from .events import COUNTED_ABSENCE_REASON, InjuryEvent, PlayerHistory

__all__ = [
    "AGE_BAND_YEARS",
    "MIN_COHORT",
    "MIN_SAMPLE_EVENTS",
    "PRE_INJURY_BASELINE_GAMES",
    "PRODUCTION_WEEKS",
    "TRAJECTORY_WEEKS",
    "age_adjusted_availability_rate",
    "age_years",
    "availability_rate",
    "body_part_history",
    "distribution",
    "median_days_to_return_by_body_part",
    "post_return_production_delta",
    "post_return_snap_trajectory",
    "resolved_events",
    "sample_size_events",
    "soft_tissue_recurrence_rate",
]

# Resolved events below which every derived rate is null. Three is the smallest
# number at which a median is not simply one of the two observations.
MIN_SAMPLE_EVENTS = int(os.getenv("MIN_SAMPLE_EVENTS", "3"))

# Same-position players within the age band needed before an age-adjusted rate
# is computed at all.
MIN_COHORT = int(os.getenv("MIN_AGE_COHORT", "5"))

# Half-width of the "equal age" band, in years. 1.5 rather than an exact-age
# match because exact ages produce cohorts of one or two at every position.
AGE_BAND_YEARS = float(os.getenv("AGE_BAND_YEARS", "1.5"))

# Games before the onset that define the pre-injury baseline the trajectory is
# expressed as a fraction of.
PRE_INJURY_BASELINE_GAMES = int(os.getenv("PRE_INJURY_BASELINE_GAMES", "4"))

# The spec's windows: snap trajectory for weeks +1..+4 after return, production
# delta for weeks +1..+2.
TRAJECTORY_WEEKS = 4
PRODUCTION_WEEKS = 2

DAYS_PER_YEAR = 365.25


def age_years(birth_date: date | None, as_of: date) -> float | None:
    """Age in decimal years, or `None`.

    `None` for an unknown birth date and for one after `as_of` — a player born
    next year is a feed error, and turning it into a negative age would rank
    them at the bottom of every cohort rather than out of all of them.
    """
    if birth_date is None or birth_date > as_of:
        return None
    return round((as_of - birth_date).days / DAYS_PER_YEAR, 2)


def availability_rate(games_possible: int, games_missed_injury: int) -> float | None:
    """`1 - missed/possible`, or `None` when nothing was possible.

    Never 1.0 for a player with no observed games — see the module docstring,
    refusal 2. A perfect availability rate is a claim about games played, and
    zero games played supports no such claim.
    """
    if games_possible <= 0:
        return None
    return round(1.0 - (games_missed_injury / games_possible), 4)


def resolved_events(events) -> list[InjuryEvent]:
    """The events with a `days_to_return` — the only ones an aggregate uses.

    An unresolved event is a player who has not played since; it is real, it is
    published in `injury_events`, and it cannot contribute a return time.
    """
    return [event for event in events if event.resolved]


def sample_size_events(events) -> int:
    """The spec's `sample_size_events`: resolved events backing the aggregates."""
    return len(resolved_events(events))


def body_part_history(events) -> dict[str, dict]:
    """`body_part -> {event_count, total_games_missed, last_onset_date}`.

    Raw counts, published at every sample size. This is not a derived rate — it
    is the evidence, and the spec's floor applies to conclusions drawn from
    evidence rather than to the evidence itself.
    """
    history: dict[str, dict] = {}
    for event in sorted(events, key=lambda e: (e.onset_date, e.event_id)):
        entry = history.setdefault(
            event.body_part,
            {"event_count": 0, "total_games_missed": 0, "last_onset_date": None},
        )
        entry["event_count"] += 1
        entry["total_games_missed"] += event.games_missed
        entry["last_onset_date"] = event.onset_date.isoformat()
    return history


def soft_tissue_recurrence_rate(events) -> float | None:
    """Recurrent soft-tissue events / total soft-tissue events.

    `None` below the sample floor, and `None` when there are no soft-tissue
    events at all: a player who has never had one has no recurrence rate, and
    0.0 would read as "has had them and never re-aggravated".
    """
    if sample_size_events(events) < MIN_SAMPLE_EVENTS:
        return None
    soft = [event for event in events if event.tissue_class == "soft_tissue"]
    if not soft:
        return None
    recurrent = sum(1 for event in soft if event.is_recurrence_of is not None)
    return round(recurrent / len(soft), 4)


def median_days_to_return_by_body_part(events) -> dict[str, float] | None:
    """`body_part -> median days_to_return`, or `None` below the sample floor.

    The whole map goes null rather than each entry, because the floor is a
    statement about the player's whole history: publishing one body part's
    median off its single event while suppressing the rest would be the same
    unsupported number with extra structure around it.
    """
    resolved = resolved_events(events)
    if len(resolved) < MIN_SAMPLE_EVENTS:
        return None
    by_part: dict[str, list[int]] = {}
    for event in resolved:
        by_part.setdefault(event.body_part, []).append(event.days_to_return)
    # `float()`, not just `round()`: `statistics.median` of a single-element int
    # list returns an int, so one body part would publish `9` and another `9.0`
    # for the same quantity. A consumer type-switching on that is a bug waiting
    # for a player with exactly one event at one body part.
    return {
        body_part: round(float(statistics.median(values)), 2)
        for body_part, values in sorted(by_part.items())
    }


def age_adjusted_availability_rate(
    rate: float | None,
    *,
    position: str,
    age: float | None,
    population: list[tuple[str, float, float]],
) -> float | None:
    """`rate` relative to same-position players of about the same age.

    1.0 means "exactly as available as the cohort", above 1.0 means more
    available. A ratio rather than a difference because availability rates live
    in a narrow band near 1.0, where a difference of 0.04 is large and reads as
    small.

    `population` is `(position, age, availability_rate)` for every player this
    pass captured a rate and an age for — see the module docstring for why it is
    the pass's own population.

    `None` whenever the comparison cannot be made: no rate, no age, a cohort
    below `MIN_COHORT`, or a cohort whose mean availability is zero.
    """
    if rate is None or age is None:
        return None
    cohort = [
        value
        for other_position, other_age, value in population
        if other_position == position and abs(other_age - age) <= AGE_BAND_YEARS
    ]
    if len(cohort) < MIN_COHORT:
        return None
    mean = statistics.fmean(cohort)
    if mean <= 0:
        return None
    return round(rate / mean, 4)


def _tenure_index(history: PlayerHistory, season: int, week: int) -> int | None:
    for index, entry in enumerate(history.tenure):
        if entry.game.season == season and entry.game.week == week:
            return index
    return None


def _baseline_snap_share(history: PlayerHistory, onset_index: int) -> float | None:
    """Mean snap share over the played games immediately before the onset."""
    shares = [
        entry.snap_pct
        for entry in reversed(history.tenure[:onset_index])
        if entry.played and entry.snap_pct is not None
    ][:PRE_INJURY_BASELINE_GAMES]
    if not shares:
        return None
    mean = statistics.fmean(shares)
    return mean if mean > 0 else None


def _return_index(history: PlayerHistory, event: InjuryEvent) -> int | None:
    """Index of the game the player returned in — the `+1` of the trajectory."""
    if event.resolved_date is None:
        return None
    for index, entry in enumerate(history.tenure):
        if entry.played and entry.game.gameday == event.resolved_date:
            return index
    return None


def post_return_snap_trajectory(history: PlayerHistory) -> list[float | None] | None:
    """Mean snap share as a fraction of the pre-injury baseline, weeks +1..+4.

    `None` (the whole array) below the sample floor. Within it, an individual
    offset is `None` when no resolved event has a game at that distance — a
    player whose only two returns came in the last fortnight of a season has no
    `+4` observation, and a zero there would read as "took no snaps".
    """
    resolved = resolved_events(history.events)
    if len(resolved) < MIN_SAMPLE_EVENTS:
        return None

    buckets: list[list[float]] = [[] for _ in range(TRAJECTORY_WEEKS)]
    for event in resolved:
        onset_index = _tenure_index(history, event.onset_season, event.onset_week)
        return_index = _return_index(history, event)
        if onset_index is None or return_index is None:
            continue
        baseline = _baseline_snap_share(history, onset_index)
        if baseline is None:
            continue
        for offset in range(TRAJECTORY_WEEKS):
            index = return_index + offset
            if index >= len(history.tenure):
                break
            entry = history.tenure[index]
            if entry.played and entry.snap_pct is not None:
                buckets[offset].append(entry.snap_pct / baseline)

    if not any(buckets):
        return None
    return [
        round(statistics.fmean(values), 4) if values else None for values in buckets
    ]


def post_return_production_delta(
    history: PlayerHistory,
    *,
    gsis_id: str,
    points: dict[tuple[str, int, int], float],
) -> float | None:
    """Mean PPR points in weeks +1..+2 after return, minus the pre-injury mean.

    A **difference in points**, not a ratio: the spec calls it a delta, and a
    ratio against a baseline near zero — every backup, every kicker week — is
    unbounded in a way a projection cannot absorb.

    `None` below the sample floor, and `None` when the production feed supplied
    no points for either side of the comparison. A missing weekly-stats season
    lands here rather than being silently treated as zero points scored.
    """
    resolved = resolved_events(history.events)
    if len(resolved) < MIN_SAMPLE_EVENTS:
        return None

    deltas: list[float] = []
    for event in resolved:
        onset_index = _tenure_index(history, event.onset_season, event.onset_week)
        return_index = _return_index(history, event)
        if onset_index is None or return_index is None:
            continue

        before = [
            points[(gsis_id, entry.game.season, entry.game.week)]
            for entry in reversed(history.tenure[:onset_index])
            if entry.played and (gsis_id, entry.game.season, entry.game.week) in points
        ][:PRE_INJURY_BASELINE_GAMES]
        after = [
            points[(gsis_id, entry.game.season, entry.game.week)]
            for entry in history.tenure[return_index : return_index + PRODUCTION_WEEKS]
            if entry.played and (gsis_id, entry.game.season, entry.game.week) in points
        ]
        if not before or not after:
            continue
        deltas.append(statistics.fmean(after) - statistics.fmean(before))

    if not deltas:
        return None
    return round(statistics.fmean(deltas), 4)


def injury_absence_invariant_holds(history: PlayerHistory) -> bool:
    """The spec's assertion, as a predicate rather than a comment.

    "The sum of injury-attributed missed games never exceeds the count of games
    where the injury report carried a designation for that player." It holds by
    construction — `classify_absence(None)` is `undesignated` and only
    `injury` is counted — and that is exactly why it is worth asserting: it is
    the property that breaks first if anybody ever makes absence itself imply an
    injury. `capture.py` records a violation as a priority error and on its own
    metric rather than trusting it silently.
    """
    return history.games_missed_injury <= history.designated_games


def counted_injury_absences(history: PlayerHistory) -> int:
    """Injury-attributed missed games, recounted from the published absences.

    Deliberately recomputed from `history.absences` rather than read off
    `history.games_missed_injury`: the two are maintained in the same loop, and
    a test that compares them is comparing a number to itself. This is what
    `capture.py` publishes, so a consumer summing the `absence_reason` values in
    `injury_history` gets the same total the profile reports.
    """
    return sum(
        1
        for absence in history.absences
        if absence.absence_reason == COUNTED_ABSENCE_REASON
    )


def distribution(values: list[float]) -> dict:
    """Count and quantiles for a set of observations.

    `None` quantiles rather than an omitted key at count 0 or 1, so a consumer
    reading the extra route can tell "no observations" from "the field is not
    published".
    """
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return {
            "count": 0, "min": None, "p25": None, "median": None,
            "p75": None, "max": None, "mean": None,
        }  # fmt: skip
    if count == 1:
        only = ordered[0]
        return {
            "count": 1, "min": only, "p25": None, "median": only,
            "p75": None, "max": only, "mean": only,
        }  # fmt: skip
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    return {
        "count": count,
        "min": ordered[0],
        "p25": round(quartiles[0], 2),
        "median": round(statistics.median(ordered), 2),
        "p75": round(quartiles[2], 2),
        "max": ordered[-1],
        "mean": round(statistics.fmean(ordered), 2),
    }

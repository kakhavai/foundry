"""The derived aggregates, and — mostly — the three refusals.

`derive.py` exists as much to decline a number as to compute one. A confident
`soft_tissue_recurrence_rate: 0.50` off two events is a coin flip wearing a
decimal point, and nothing downstream can tell it from a real rate. These tests
pin the refusals, because a refusal that quietly stops refusing produces
plausible output and breaks nothing visible.
"""

from datetime import date

import httpx
import pytest
import respx

from durability_history import derive
from durability_history.capture import (
    DURABILITY_PROFILE,
    RETURN_TRAJECTORY,
    capture_durability_history,
)
from durability_history.events import InjuryEvent

from .conftest import (
    ALPHA,
    BRAVO,
    CANONICAL_IDS,
    NOW,
    SEASON,
    WEEK,
    mock_identity,
    mock_upstreams,
)


def event(
    *,
    site: str = "hamstring",
    tissue: str = "soft_tissue",
    days: int | None = 14,
    recurrence: str | None = None,
    onset: date = date(2026, 9, 10),
) -> InjuryEvent:
    return InjuryEvent(
        event_id=f"e:{site}:{onset.isoformat()}",
        body_part=site if site in {"hamstring", "knee"} else "other",
        injury_site=site,
        tissue_class=tissue,
        onset_date=onset,
        onset_season=2026,
        onset_week=2,
        games_missed=1,
        days_to_return=days,
        resolved_date=None if days is None else onset,
        is_recurrence_of=recurrence,
    )


# ── refusal 1: below the sample floor, the derived rates are null ────────────


def test_below_the_sample_floor_the_derived_rates_are_null():
    """The spec: "aggregates with `sample_size_events` below the configured floor
    are emitted with the raw events but with the derived rates null"."""
    two = [event(recurrence="x"), event(onset=date(2026, 10, 1))]
    assert derive.sample_size_events(two) == 2 < derive.MIN_SAMPLE_EVENTS
    assert derive.soft_tissue_recurrence_rate(two) is None
    assert derive.median_days_to_return_by_body_part(two) is None


def test_at_the_sample_floor_the_derived_rates_appear():
    """The floor must not be so high that nothing is ever published — a refusal
    that never lifts is indistinguishable from a broken computation."""
    three = [
        event(recurrence="x"),
        event(onset=date(2026, 10, 1)),
        event(onset=date(2026, 11, 1)),
    ]
    assert derive.sample_size_events(three) == derive.MIN_SAMPLE_EVENTS
    assert derive.soft_tissue_recurrence_rate(three) == pytest.approx(1 / 3, abs=1e-4)
    assert derive.median_days_to_return_by_body_part(three) == {"hamstring": 14.0}


def test_unresolved_events_do_not_count_toward_the_sample():
    """Three events of which two never resolved is a sample of one, and treating
    it as three is how a two-week median gets published off one observation."""
    events = [event(), event(days=None), event(days=None)]
    assert derive.sample_size_events(events) == 1
    assert derive.median_days_to_return_by_body_part(events) is None


def test_a_player_with_no_soft_tissue_events_has_no_recurrence_RATE():
    """`None`, not 0.0. A player who has never had a soft-tissue injury has no
    recurrence rate; 0.0 would read as "has had them and never re-aggravated",
    which is a claim about a population of zero."""
    joints = [event(site="knee", tissue="joint") for _ in range(4)]
    assert derive.sample_size_events(joints) >= derive.MIN_SAMPLE_EVENTS
    assert derive.soft_tissue_recurrence_rate(joints) is None


def test_the_trajectory_is_null_below_the_floor_even_when_it_COULD_be_computed():
    """The refusal that is invisible unless the data would otherwise support a
    number.

    A player with zero resolved events gets `None` either way — the buckets come
    out empty — so a fixture built only from clean and unresolved histories cannot
    tell "we refused" from "there was nothing to compute". Mutation testing found
    exactly that: deleting the floor check changed nothing. This builds a history
    with TWO resolved returns and real snap shares, where the mutation publishes a
    four-week trajectory off two observations.
    """
    from .test_events import build, designations

    history = build(
        played=[1, 3, 4, 6, 7, 8],
        designated=designations((2, "Hamstring"), (5, "Hamstring")),
    )
    assert derive.sample_size_events(history.events) == 2 < derive.MIN_SAMPLE_EVENTS
    # The evidence a mutation would happily publish: two real returns, each with
    # a pre-injury baseline and post-return snap shares behind it.
    assert all(event.resolved for event in history.events)
    assert history.tenure[0].snap_pct is not None

    # Points for every played week, so the delta would compute too.
    points = {
        ("x", entry.game.season, entry.game.week): 10.0
        for entry in history.tenure
        if entry.played
    }
    assert points, "the fixture supplied no production to refuse"

    assert derive.post_return_snap_trajectory(history) is None
    assert (
        derive.post_return_production_delta(history, gsis_id="x", points=points) is None
    )


def test_the_median_map_is_null_as_a_WHOLE_below_the_floor():
    """Not per body part. Publishing one body part's median off its single event
    while suppressing the rest is the same unsupported number with extra
    structure around it."""
    mixed = [event(), event(site="knee", tissue="joint")]
    assert derive.median_days_to_return_by_body_part(mixed) is None


# ── refusal 2: zero games possible is null, never 1.0 ────────────────────────


def test_availability_with_zero_games_possible_is_null_not_perfect():
    """`1 - 0/0` reading as a perfect record is the same class of error as
    `Coverage.ratio` returning 1.0 for `expected: 0`."""
    assert derive.availability_rate(0, 0) is None


def test_availability_is_a_real_rate_when_there_are_games():
    assert derive.availability_rate(18, 3) == pytest.approx(0.8333, abs=1e-4)
    assert derive.availability_rate(18, 0) == 1.0
    assert derive.availability_rate(4, 4) == 0.0


# ── refusal 3: below the cohort floor, no age adjustment ─────────────────────


def test_age_adjustment_below_the_cohort_floor_is_null():
    """Ranking a 28-year-old tight end against the two other 28-year-old tight
    ends this pass captured produces a confident-looking ratio with no population
    behind it."""
    tiny = [("TE", 28.0, 0.9), ("TE", 28.4, 0.8)]
    assert (
        derive.age_adjusted_availability_rate(
            0.9, position="TE", age=28.0, population=tiny
        )
        is None
    )


def test_age_adjustment_uses_same_position_players_inside_the_age_band():
    cohort = [("RB", 25.0, 0.8) for _ in range(derive.MIN_COHORT)]
    # A different position and an out-of-band age, both of which must be ignored.
    noise = [("WR", 25.0, 0.2), ("RB", 40.0, 0.1)]
    adjusted = derive.age_adjusted_availability_rate(
        0.9, position="RB", age=25.0, population=cohort + noise
    )
    assert adjusted == pytest.approx(0.9 / 0.8, abs=1e-4)


def test_age_adjustment_is_null_without_an_age_or_a_rate():
    cohort = [("RB", 25.0, 0.8) for _ in range(derive.MIN_COHORT)]
    assert (
        derive.age_adjusted_availability_rate(
            0.9, position="RB", age=None, population=cohort
        )
        is None
    )
    assert (
        derive.age_adjusted_availability_rate(
            None, position="RB", age=25.0, population=cohort
        )
        is None
    )


def test_age_years_refuses_a_birth_date_after_the_as_of_date():
    """A player born next year is a feed error. Turning it into a negative age
    ranks them at the bottom of every cohort rather than out of all of them."""
    assert derive.age_years(date(2030, 1, 1), date(2026, 1, 1)) is None
    assert derive.age_years(None, date(2026, 1, 1)) is None
    assert derive.age_years(date(2000, 1, 1), date(2026, 1, 1)) == pytest.approx(
        26.0, abs=0.05
    )


# ── the distribution helper the extra route publishes ───────────────────────


def test_distribution_reports_nulls_rather_than_omitting_keys():
    """A consumer must be able to tell "no observations" from "the field is not
    published", so the shape never changes."""
    empty = derive.distribution([])
    assert empty["count"] == 0
    assert set(empty) == {"count", "min", "p25", "median", "p75", "max", "mean"}
    assert all(empty[key] is None for key in empty if key != "count")

    one = derive.distribution([10.0])
    assert one["count"] == 1 and one["median"] == 10.0 and one["p25"] is None

    many = derive.distribution([10.0, 20.0, 30.0, 40.0])
    assert many["count"] == 4 and many["median"] == 25.0 and many["max"] == 40.0


# ── the refusals, end to end ────────────────────────────────────────────────


@respx.mock
async def test_a_low_sample_player_publishes_events_but_null_rates(lake):
    """The spec's exact shape: "emitted with the raw events but with the derived
    rates null". Delta has one unresolved ankle event."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    async with httpx.AsyncClient() as client:
        envelopes = await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW
        )

    from .conftest import DELTA

    profile = next(
        r
        for r in envelopes[DURABILITY_PROFILE].signals
        if r["player_id"] == CANONICAL_IDS[DELTA]
    )
    assert profile["sample_size_events"] == 0
    assert profile["soft_tissue_recurrence_rate"] is None
    assert profile["median_days_to_return_by_body_part"] is None
    # ...but the raw evidence is still there.
    assert profile["body_part_history"] == {
        "ankle": {
            "event_count": 1,
            "total_games_missed": 1,
            "last_onset_date": "2026-09-12",
        }
    }


@respx.mock
async def test_a_player_at_the_floor_publishes_a_real_trajectory(lake):
    """Bravo has three resolved events and comes back at reduced usage after each
    one — which is the whole thing `post_return_snap_trajectory` measures."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    async with httpx.AsyncClient() as client:
        envelopes = await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW
        )

    row = next(
        r
        for r in envelopes[RETURN_TRAJECTORY].signals
        if r["player_id"] == CANONICAL_IDS[BRAVO]
    )
    assert row["sample_size_events"] == 3
    trajectory = row["post_return_snap_trajectory"]
    assert trajectory is not None and len(trajectory) == 4
    assert trajectory[0] < 1.0, "the return week should be below the baseline"
    assert trajectory[0] < trajectory[1], "usage should climb back toward baseline"
    assert row["post_return_production_delta"] < 0


@respx.mock
async def test_a_clean_player_publishes_nulls_rather_than_a_perfect_score(lake):
    """Alpha has never been hurt. `soft_tissue_recurrence_rate: 0.0` would claim
    he has had soft-tissue injuries and never re-aggravated one."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    async with httpx.AsyncClient() as client:
        envelopes = await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW
        )

    row = next(
        r
        for r in envelopes[DURABILITY_PROFILE].signals
        if r["player_id"] == CANONICAL_IDS[ALPHA]
    )
    assert row["availability_rate"] == 1.0
    assert row["soft_tissue_recurrence_rate"] is None
    assert row["body_part_history"] == {}
    assert row["sample_size_events"] == 0

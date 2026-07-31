"""The polling-window arithmetic that `coverage.expected` is built from.

Every assertion here is really the same assertion: the expectation comes from
the calendar and the clock, and nothing the upstream returns can move it.
"""

from datetime import UTC, datetime, timedelta

import pytest

from roster_transactions.windows import (
    INTERVAL,
    INTERVALS_PER_WEEK,
    covered_interval_keys,
    elapsed_interval_keys,
    interval_key,
    week_window,
)


def test_a_week_is_672_fifteen_minute_intervals():
    assert INTERVAL == timedelta(minutes=15)
    assert INTERVALS_PER_WEEK == 7 * 24 * 4 == 672


def test_week_one_starts_on_a_tuesday():
    """Anchored to the league's transaction week, not to kickoff: the moves that
    matter for a week happen days before anybody plays."""
    start, end = week_window(2026, 1)
    assert start.weekday() == 1, "Tuesday is weekday 1"
    assert start.tzinfo is not None
    assert end - start == timedelta(weeks=1)


def test_consecutive_weeks_abut_without_gap_or_overlap():
    """A gap would make some intervals belong to no week, and an overlap would
    make a transaction land in two."""
    for week in range(1, 18):
        _, end = week_window(2026, week)
        next_start, _ = week_window(2026, week + 1)
        assert end == next_start, week


def test_a_week_below_one_is_refused():
    with pytest.raises(ValueError):
        week_window(2026, 0)


def test_only_fully_elapsed_intervals_are_expected():
    """An interval in progress cannot have been acknowledged by anybody.
    Counting it would put every pass permanently one interval short and make a
    `< 1.0` alert useless."""
    start, _ = week_window(2026, 1)
    mid_interval = start + timedelta(minutes=22)
    assert elapsed_interval_keys(2026, 1, mid_interval) == [interval_key(start)]


def test_nothing_has_elapsed_at_the_instant_a_week_opens():
    start, _ = week_window(2026, 1)
    assert elapsed_interval_keys(2026, 1, start) == []


def test_a_future_week_expects_nothing_which_is_why_capture_floors_it():
    start, _ = week_window(2026, 5)
    assert elapsed_interval_keys(2026, 5, start - timedelta(days=30)) == []


def test_a_completed_week_expects_the_whole_universe():
    _, end = week_window(2026, 3)
    keys = elapsed_interval_keys(2026, 3, end + timedelta(days=99))
    assert len(keys) == INTERVALS_PER_WEEK
    assert len(set(keys)) == INTERVALS_PER_WEEK, "interval keys must be unique"


def test_elapsed_intervals_do_not_depend_on_anything_the_upstream_said():
    """The whole point, stated as a test: the only inputs are season, week and
    the clock."""
    now = week_window(2026, 2)[0] + timedelta(days=3)
    assert elapsed_interval_keys(2026, 2, now) == elapsed_interval_keys(2026, 2, now)
    assert len(elapsed_interval_keys(2026, 2, now)) == 3 * 24 * 4


def test_interval_keys_are_stable_across_passes():
    """An unstable key makes every interval look newly missing every fifteen
    minutes."""
    start, _ = week_window(2026, 1)
    first = elapsed_interval_keys(2026, 1, start + timedelta(hours=5))
    later = elapsed_interval_keys(2026, 1, start + timedelta(hours=9))
    assert len(first) == 20
    assert later[: len(first)] == first


def test_covered_never_exceeds_elapsed():
    """A manifest claiming the future must not inflate `present` past
    `expected` — that would read as better than complete."""
    start, _ = week_window(2026, 1)
    now = start + timedelta(days=1)
    covered = covered_interval_keys(2026, 1, now, now + timedelta(days=30))
    assert covered == elapsed_interval_keys(2026, 1, now)
    assert len(covered) == 96


def test_a_partial_acknowledgement_covers_only_its_own_span():
    start, _ = week_window(2026, 1)
    now = start + timedelta(days=4)
    covered = covered_interval_keys(2026, 1, now, start + timedelta(days=1))
    assert len(covered) == 96
    assert len(elapsed_interval_keys(2026, 1, now)) == 384
    assert set(covered) < set(elapsed_interval_keys(2026, 1, now))


def test_an_acknowledgement_before_the_week_covers_nothing():
    start, _ = week_window(2026, 1)
    assert covered_interval_keys(2026, 1, start + timedelta(days=2), start) == []


def test_interval_keys_are_namespaced_and_readable():
    """`coverage.missing` mixes interval keys with row keys for unplaceable
    rows, so a reader has to be able to tell them apart."""
    key = interval_key(datetime(2026, 9, 1, 6, 15, tzinfo=UTC))
    assert key == "interval:2026-09-01T06:15:00Z"

from datetime import UTC, datetime, timedelta

from collector_core.cadence import CadenceClass, next_interval

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)


def test_base_interval_for_each_class():
    assert next_interval(CadenceClass.VOLATILE, now=NOW) == timedelta(minutes=15)
    assert next_interval(CadenceClass.PERISHABLE, now=NOW) == timedelta(minutes=5)
    assert next_interval(CadenceClass.WEEKLY, now=NOW) == timedelta(days=1)
    assert next_interval(CadenceClass.SEASONAL, now=NOW) == timedelta(days=1)
    assert next_interval(CadenceClass.STATIC_REFERENCE, now=NOW) == timedelta(days=1)


def test_escalates_inside_the_window():
    """T-90min before kickoff, weather switches from 15 min to 5 min."""
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(minutes=45),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=5)


def test_does_not_escalate_outside_the_window():
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(hours=6),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=15)


def test_escalation_boundary_is_inclusive():
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(minutes=90),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=5)


def test_still_escalated_while_the_event_is_in_progress():
    """A game underway is a negative delta. The dense cadence must persist —
    the final whistle, not kickoff, ends the window."""
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW - timedelta(minutes=40),
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=5)


def test_no_upcoming_event_falls_back_to_base():
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=None,
        escalate_within=timedelta(minutes=90),
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=15)


def test_no_escalation_window_falls_back_to_base():
    """Missing escalate_within triggers guard clause — falls back to base."""
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(minutes=45),
        escalate_within=None,
        escalated_interval=timedelta(minutes=5),
    )
    assert interval == timedelta(minutes=15)


def test_no_escalated_interval_falls_back_to_base():
    """Missing escalated_interval triggers guard clause — falls back to base."""
    interval = next_interval(
        CadenceClass.VOLATILE,
        now=NOW,
        next_event_at=NOW + timedelta(minutes=45),
        escalate_within=timedelta(minutes=90),
        escalated_interval=None,
    )
    assert interval == timedelta(minutes=15)

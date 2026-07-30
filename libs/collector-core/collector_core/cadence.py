"""Cadence is a declared property of a collector, not an ad-hoc number.

Declaring it means `collector_staleness_seconds` can be alerted against the
class uniformly, instead of twenty-six bespoke thresholds.
"""

from datetime import datetime, timedelta
from enum import StrEnum


class CadenceClass(StrEnum):
    STATIC_REFERENCE = "static reference"
    SEASONAL = "seasonal"
    WEEKLY = "weekly"
    VOLATILE = "volatile"
    PERISHABLE = "perishable"


BASE_INTERVALS: dict[CadenceClass, timedelta] = {
    CadenceClass.STATIC_REFERENCE: timedelta(days=1),
    CadenceClass.SEASONAL: timedelta(days=1),
    CadenceClass.WEEKLY: timedelta(days=1),
    CadenceClass.VOLATILE: timedelta(minutes=15),
    CadenceClass.PERISHABLE: timedelta(minutes=5),
}


def next_interval(
    cadence_class: CadenceClass,
    *,
    now: datetime,
    next_event_at: datetime | None = None,
    escalate_within: timedelta | None = None,
    escalated_interval: timedelta | None = None,
) -> timedelta:
    """Interval until the next capture, honouring an escalation window.

    A negative delta — the event already started — stays escalated. The window
    closes when the caller stops supplying the event, not at kickoff, because
    conditions during play are the densest part of the series.
    """
    base = BASE_INTERVALS[cadence_class]
    if next_event_at is None or escalate_within is None or escalated_interval is None:
        return base
    if next_event_at - now <= escalate_within:
        return escalated_interval
    return base

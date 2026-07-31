"""schedule-context's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that
answer "is THIS collector wrong in the way only it can be wrong" belong here.

The two below are exactly that.

`schedule_context_games_in_scope` catches the failure `collector_coverage_ratio`
structurally cannot see. Coverage is computed against a floor, and the floor is
a *lower* bound — a week that legitimately runs 13 games and a week whose feed
lost three both report 1.0 once the observed count clears the floor. The raw
game count is what makes "this week suddenly has fewer games than last week"
queryable.

`schedule_context_unresolved_venues` counts team-records whose venue could not
be determined — an unrecognised neutral-site stadium name is the realistic
cause, and it arrives silently when the league announces a new international
venue. Those rows are dropped from `game_situational_context` with a reason,
so the coverage ratio does move; the counter is what says *which* failure it
was without reading every envelope's errors array.

Both record on every pass, including zero. An absent Prometheus series and a
healthy one are indistinguishable in PromQL, so a gauge written only when it
is interesting cannot be alerted on.

`ScheduleContextMetrics` is still a `CollectorMetrics`, so it satisfies
`CollectorDescriptor.metrics` unchanged. Exactly one instance exists per
process — the library never constructs one, it takes the one below.
"""

from collector_core.metrics import CollectorMetrics
from opentelemetry import metrics as otel_metrics

COLLECTOR = "schedule-context"


class ScheduleContextMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._games_in_scope = meter.create_gauge(
            "schedule_context_games_in_scope",
            description="Distinct games the scoped week resolved to, by collector.",
        )
        self._unresolved_venues = meter.create_gauge(
            "schedule_context_unresolved_venues",
            description=(
                "Team-records in the scoped week whose venue could not be "
                "resolved, by collector."
            ),
        )

    def games_in_scope(self, count: int) -> None:
        self._games_in_scope.set(count, {"collector": self.collector})

    def unresolved_venues(self, count: int) -> None:
        self._unresolved_venues.set(count, {"collector": self.collector})


metrics = ScheduleContextMetrics()

"""durability-history's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)`: the fleet-wide
series (`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that answer
"is THIS collector wrong in the way only it can be wrong" belong here.

Two of the four earn the subclass, and both exist because
`collector_coverage_ratio` **cannot see this collector's named failure mode at
all**. A player whose eight suspension games were folded into
`career_games_missed_injury` produces a complete, well-formed, fully-covered
record. Coverage reads 1.0. The availability rate is a believable number that is
simply wrong, and every downstream projection for a player who has never been
hurt inherits it.

* `durability_history_undesignated_absences` — games a scoped player missed with
  NO designation to source a reason from. It is the volume of absence this
  collector deliberately refuses to attribute, and it is the number that moves
  first if the upstream stops publishing report lines: a sudden climb means the
  injury feed went quiet, not that the league got healthier.
* `durability_history_attribution_violations` — players whose injury-attributed
  misses exceeded the games they carried a designation for. It is the spec's
  assertion, and it should be 0 forever; anything else is a code bug that has
  already fabricated durability problems.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a capture
cadence is absent from every scrape that does not immediately follow one — and
PromQL cannot tell an absent series from a healthy idle one. On a `seasonal`
cadence, where a pass runs once a day and Prometheus scrapes every fifteen
seconds, roughly one scrape in 5,760 would carry a value. Counters need none of
this.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "durability-history"


class DurabilityHistoryMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "durability_history_rows_captured",
            description="Durability records captured in the last pass, by collector.",
        )
        self._undesignated_absences = LastValueGauge(
            meter,
            "durability_history_undesignated_absences",
            description=(
                "Games a scoped player missed with no injury-report designation "
                "to source a reason from, and which are therefore deliberately "
                "not attributed to injury."
            ),
        )
        self._attribution_violations = LastValueGauge(
            meter,
            "durability_history_attribution_violations",
            description=(
                "Scoped players whose injury-attributed missed games exceeded "
                "the games they carried an injury-report designation for. The "
                "spec's assertion; expected to be zero forever."
            ),
        )
        self._unresolved_scope_slots = LastValueGauge(
            meter,
            "durability_history_unresolved_scope_slots",
            description=(
                "Scope members no resolved upstream row covered in the last pass."
            ),
        )

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero."""
        self._rows_captured.set(count, {"collector": self.collector})

    def undesignated_absences(self, count: int) -> None:
        """The absences this collector refused to call injuries.

        Recorded on every pass including zero — which is the whole point. A gauge
        written only when it is interesting cannot distinguish "every absence was
        explained" from "this collector stopped running", and those are exactly
        the two states an operator needs to tell apart here.
        """
        self._undesignated_absences.set(count, {"collector": self.collector})

    def attribution_violations(self, count: int) -> None:
        """The spec's assertion, counted. Record every pass, including zero."""
        self._attribution_violations.set(count, {"collector": self.collector})

    def unresolved_scope_slots(self, count: int) -> None:
        """Watchlist players no upstream row covered. Record every pass."""
        self._unresolved_scope_slots.set(count, {"collector": self.collector})


metrics = DurabilityHistoryMetrics()

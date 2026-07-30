"""roster-scope's binding to the shared collector metrics, plus the two
series that are genuinely this collector's own.

Longer than weather's six-line equivalent, and deliberately so. The Phase 8
spec's failure mode for this collector — "the scope is wrong and reports
itself as complete" — is invisible to `collector_coverage_ratio` by
construction, because coverage is computed against the same config that
drove the fetch. Catching it needs assertions external to scope, and these
are the two.
"""

from collector_core.metrics import CollectorMetrics
from opentelemetry import metrics as otel_metrics

COLLECTOR = "roster-scope"


class RosterScopeMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        # OTel appends `_total`: this renders as `scope_missed_producers_total`.
        self._missed_producers = meter.create_counter(
            "scope_missed_producers",
            description=(
                "player_id values that recorded nonzero usage while their "
                "membership_status was excluded. Alert on any nonzero value."
            ),
        )
        self._stale_depth_charts = meter.create_gauge(
            "roster_scope_stale_depth_charts",
            description=(
                "Teams whose depth_source_captured_at is older than the "
                "48-hour freshness invariant, or absent entirely."
            ),
        )

    def missed_producers(self, count: int) -> None:
        """Record the reconciliation result — including zero.

        Recorded on every capture rather than only when nonzero: an absent
        series and a healthy one are indistinguishable in PromQL, and this
        metric's entire job is to be alertable on `> 0`.
        """
        self._missed_producers.add(count, {"collector": self.collector})

    def stale_depth_charts(self, count: int) -> None:
        self._stale_depth_charts.set(count, {"collector": self.collector})


metrics = RosterScopeMetrics()

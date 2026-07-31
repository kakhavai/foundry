"""injury-report's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`, `collector_staleness_
seconds`) belong to the library, and the series that answer "is THIS collector
wrong in the way only it can be wrong" belong here. A metric only one service
records must not grow into the shared library — see player-identity's
`identity_merge_conflicts` and roster-scope's `scope_missed_producers` for what
that looks like when it is real.

`InjuryReportMetrics` is still a `CollectorMetrics`, so it satisfies
`CollectorDescriptor.metrics` unchanged. Exactly one instance exists per
process — the library never constructs one, it takes the one below.
"""

from collector_core.metrics import CollectorMetrics
from opentelemetry import metrics as otel_metrics

COLLECTOR = "injury-report"


class InjuryReportMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        # PLACEHOLDER. Replace with the series that make THIS collector's own
        # failure mode visible — the one `collector_coverage_ratio` cannot see
        # because coverage is computed against the same input that drove the
        # fetch. If there genuinely is no such series, delete the subclass and
        # use `metrics = CollectorMetrics(COLLECTOR)` (weather does).
        self._rows_captured = meter.create_gauge(
            "injury_report_rows_captured",
            description="Rows captured in the last pass, by collector.",
        )

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable in
        PromQL, so a gauge only written when it is interesting cannot be
        alerted on.
        """
        self._rows_captured.set(count, {"collector": self.collector})


metrics = InjuryReportMetrics()

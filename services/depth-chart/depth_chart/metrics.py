"""depth-chart's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)`, and deliberately
**not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that answer
"is THIS collector wrong in the way only it can be wrong" belong here.

`depth_chart_stale_charts` is that series, and it exists because
`collector_coverage_ratio` structurally cannot see this collector's worst
failure. A vendor that freezes a preseason chart and keeps serving it produces
**perfect coverage**: all 160 groups present, every field populated and
plausible, `stability_score` converging on 1.0 as the frozen ordering "holds"
week after week. Coverage is computed against the same input that drove the
fetch, so it agrees with the lie. Freshness is the independent axis, and it has
to be its own series to be alertable.

`DepthChartMetrics` is still a `CollectorMetrics`, so it satisfies
`CollectorDescriptor.metrics` unchanged. Exactly one instance exists per process
— the library never constructs one, it takes the one below.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "depth-chart"


class DepthChartMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._stale_charts = LastValueGauge(
            meter,
            "depth_chart_stale_charts",
            description=(
                "(team, position) groups whose chart_published_at is older than "
                "the staleness threshold, by collector."
            ),
        )
        self._charted_players = LastValueGauge(
            meter,
            "depth_chart_charted_players",
            description="Charted players in the last pass, by collector.",
        )

    def stale_charts(self, count: int) -> None:
        """Record every pass, including zero.

        Zero is the interesting value here: it is what distinguishes "no team is
        serving a frozen chart" from "this gauge has never been written", and
        PromQL cannot tell an absent series from a healthy one.
        """
        self._stale_charts.set(count, {"collector": self.collector})

    def charted_players(self, count: int) -> None:
        """Record every pass, including zero. Same reasoning as above — a pass
        that captured nobody must be visible, not absent."""
        self._charted_players.set(count, {"collector": self.collector})


metrics = DepthChartMetrics()

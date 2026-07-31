"""player-stats' binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)`, and deliberately
**not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that
answer "is THIS collector wrong in the way only it can be wrong" belong here.

Both series below are named by the Phase 8 spec, and both catch something
`collector_coverage_ratio` structurally cannot:

- `player_stats_restatements_total` — the spec's own alert: "spiking outside
  the normal Monday-to-Wednesday window, which usually means an adapter is
  re-emitting unchanged rows as new revisions". Every one of those re-emissions
  is a *present* row, so coverage stays at 1.0 while the lake fills with
  fictional corrections.
- `player_stats_identity_misses` — a box-score row whose upstream id did not
  cross-walk to a canonical `player_id`. The phase doc calls this "the failure
  most likely to go unnoticed for a season": a capture that resolves 97% of
  rows looks completely healthy on every other metric.

Both are recorded on **every** pass, including zero. An absent Prometheus
series and a healthy one are indistinguishable in PromQL, so a value only
written when it is interesting cannot be alerted on.
"""

from collector_core.metrics import CollectorMetrics
from opentelemetry import metrics as otel_metrics

COLLECTOR = "player-stats"


class PlayerStatsMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        # A counter, not a gauge: the spec alerts on the *rate* of
        # restatements, and OTel renders this as
        # `player_stats_restatements_total`.
        self._restatements = meter.create_counter(
            "player_stats_restatements",
            description=(
                "Rows whose counting stats changed against the previous "
                "snapshot, by collector."
            ),
        )
        self._identity_misses = meter.create_gauge(
            "player_stats_identity_misses",
            description=(
                "Box-score rows in the last pass whose upstream id did not "
                "resolve to a canonical player_id, by collector."
            ),
        )

    def restatements(self, count: int) -> None:
        """Record every pass, including zero — `add(0)` keeps the series alive
        so a quiet week is distinguishable from a collector that stopped."""
        self._restatements.add(count, {"collector": self.collector})

    def identity_misses(self, count: int) -> None:
        self._identity_misses.set(count, {"collector": self.collector})


metrics = PlayerStatsMetrics()

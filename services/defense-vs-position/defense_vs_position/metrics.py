"""defense-vs-position's binding to the shared collector metrics.

The fleet-wide series (`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to `collector-core`. The three below
answer "is THIS collector wrong in the way only it can be wrong", which
`collector_coverage_ratio` structurally cannot see:

* **`rank_divergences`** is the named failure mode made alertable. Coverage
  counts defenses that have a full set of rows; a divergent defense HAS a full
  set of rows, every field populated and plausible, and the two rate bases
  simply disagree about it. Coverage reads 1.0 the whole time.
* **`players_resolved_ratio`** is the hole coverage cannot see either. An
  opportunity dropped for an unresolved player deflates a defense's rates
  without removing its row, so a `player-identity` outage would publish
  32 complete-looking defenses whose every number is too small.
* **`rows_captured`** is the ordinary volume series.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a
weekly cadence is absent from every scrape that does not immediately follow
one -- and PromQL cannot tell an absent series from a healthy idle one.
Counters are already cumulative and need none of this.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "defense-vs-position"


class DefenseVsPositionMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "defense_vs_position_rows_captured",
            description="Rated rows published in the last pass, by collector.",
        )
        self._rank_divergences = LastValueGauge(
            meter,
            "defense_vs_position_rank_divergences",
            description=(
                "Splits whose per-game and per-opportunity ranks differ by "
                "more than the review threshold, in the last pass."
            ),
        )
        self._players_resolved_ratio = LastValueGauge(
            meter,
            "defense_vs_position_players_resolved_ratio",
            description=(
                "Share of players with opportunities that player-identity "
                "resolved in the last pass. Below 1.0, every rate is "
                "deflated by the opportunities that were dropped."
            ),
        )

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero."""
        self._rows_captured.set(count, {"collector": self.collector})

    def rank_divergences(self, count: int) -> None:
        """Record every pass, including zero.

        Zero is as interesting as a large value: a gauge written only when the
        guard fires cannot distinguish "nothing diverged this week" from "the
        guard stopped running".
        """
        self._rank_divergences.set(count, {"collector": self.collector})

    def players_resolved(self, resolved: int, seen: int) -> None:
        """Record the resolved share, every pass.

        `seen == 0` records 1.0 rather than dividing: a week with no
        opportunities at all resolved everything it was asked to, and emitting
        0.0 would fire the identity alert on a bye.
        """
        ratio = resolved / seen if seen else 1.0
        self._players_resolved_ratio.set(ratio, {"collector": self.collector})


metrics = DefenseVsPositionMetrics()

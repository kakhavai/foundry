"""offensive-line's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that
answer "is THIS collector wrong in the way only it can be wrong" belong here.

There are three such ways and none of them is visible to
`collector_coverage_ratio`, because in all three every published row is
present, populated and plausible:

* **The opponent adjustment collapses to a constant.**
  `defense-vs-position` published an adjusted column that was the league mean
  for all 32 teams while the raw value spanned 4x, because it adjusted a unit
  by its own leave-one-out mean of the quantity being rated — and the mean of
  a unit's leave-one-out means is exactly its full mean. Coverage was 1.0
  throughout. **The tell was zero variance**, so the variance is a gauge.
* **The lineup guard stops seeing anything.** The guard raises rather than
  flags, so a pass that fires it is loud by construction — but a guard that
  quietly stops having subjects (because `lineup_hash` went null league-wide
  when a feed degraded) looks identical to a league where nobody changed a
  starter. `offensive_line_lineup_changes` is how many unit rows reported a
  changed five; a healthy in-season week is never zero for long.
* **`player-identity` refuses starters silently.** An unresolved starter is a
  missing row, and five missing rows per team read the same as a team the
  collector never saw. The counter separates them.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a
capture cadence is absent from every scrape that does not immediately follow
one — and PromQL cannot tell an absent series from a healthy idle one.
Counters are fine as they are; only gauges need the wrapper.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "offensive-line"


class OffensiveLineMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "offensive_line_rows_captured",
            description="Rows published in the last pass, by collector.",
        )
        self._adjusted_variance = LastValueGauge(
            meter,
            "offensive_line_adjusted_variance",
            description=(
                "Population variance of an opponent-adjusted column across the "
                "league in the last pass. Zero means the adjustment carries no "
                "information -- see this module's docstring."
            ),
        )
        self._lineup_changes = LastValueGauge(
            meter,
            "offensive_line_lineup_changes",
            description=(
                "Unit rows whose lineup_hash differed from the prior game's, "
                "in the last pass. The lineup guard's subject count: zero for "
                "weeks on end means the guard has nothing to check, which "
                "looks exactly like a league nobody got hurt in."
            ),
        )
        self._degraded_upstreams = LastValueGauge(
            meter,
            "offensive_line_degraded_upstreams",
            description=(
                "How many optional feeds were unavailable on the last pass. "
                "They cost the starter rows, which coverage shows -- but not "
                "which feed, which is what this answers."
            ),
        )
        self._starters_unresolved = meter.create_counter(
            "offensive_line_starters_unresolved_total",
            description=(
                "Identified starters player-identity did not resolve, so the "
                "five is short by a countable amount rather than quietly."
            ),
        )

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable in
        PromQL, so a gauge only written when it is interesting cannot be
        alerted on. `LastValueGauge` is the other half of that: it keeps the
        series present on every scrape, not only the one after this call.
        """
        self._rows_captured.set(count, {"collector": self.collector})

    def adjusted_variance(self, column: str, variance: float) -> None:
        """The variance of one adjusted column. Alert on it reaching zero."""
        self._adjusted_variance.set(
            variance, {"collector": self.collector, "column": column}
        )

    def lineup_changes(self, count: int) -> None:
        self._lineup_changes.set(count, {"collector": self.collector})

    def degraded_upstreams(self, count: int) -> None:
        self._degraded_upstreams.set(count, {"collector": self.collector})

    def starters_unresolved(self, count: int) -> None:
        if count:
            self._starters_unresolved.add(count, {"collector": self.collector})


metrics = OffensiveLineMetrics()

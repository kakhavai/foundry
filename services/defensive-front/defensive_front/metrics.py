"""defensive-front's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that
answer "is THIS collector wrong in the way only it can be wrong" belong here.

There are two such ways, and neither is visible to `collector_coverage_ratio`,
because in both of them every row is present, populated and plausible:

* **The adjustment collapses to a constant.** `defense-vs-position` published
  an opponent-adjusted column that was the league mean for all 32 teams while
  the raw value spanned 4x, because it adjusted a unit by its own
  leave-one-out mean of the quantity being rated — and the mean of a unit's
  leave-one-out means is exactly its full mean. Coverage was 1.0 throughout.
  **The tell was zero variance**, so the variance is a gauge.
* **The timing confound.** The spec's named failure mode: a front that draws
  quick-game opponents is rated average while genuinely disrupting. The
  regression's slope and t-statistic are gauges so the guard's *behaviour* is
  observable, not just its verdict — a guard that quietly stops discriminating
  looks identical to one that keeps passing.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a
capture cadence is absent from every scrape that does not immediately follow
one — and PromQL cannot tell an absent series from a healthy idle one.
Counters are fine as they are; only gauges need the wrapper.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "defensive-front"

# What a guard gauge carries when the guard could not run at all — too few
# comparable teams, or no spread in release timing. Distinct from a slope of
# zero, which is a pass, and readable in PromQL without a second series.
NOT_RUN = -1.0


class DefensiveFrontMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "defensive_front_rows_captured",
            description="Rows published in the last pass, by collector.",
        )
        self._adjusted_variance = LastValueGauge(
            meter,
            "defensive_front_adjusted_variance",
            description=(
                "Population variance of an opponent-adjusted column across the "
                "league in the last pass. Zero means the adjustment carries no "
                "information -- see this module's docstring."
            ),
        )
        self._timing_slope = LastValueGauge(
            meter,
            "defensive_front_timing_confound_slope",
            description=(
                "Residual slope of pressure_rate_generated_adj on "
                "mean_time_to_throw_faced, per second."
            ),
        )
        self._timing_t = LastValueGauge(
            meter,
            "defensive_front_timing_confound_t_statistic",
            description=(
                "t-statistic of that slope. Its magnitude past the critical "
                "value is what flags the pass."
            ),
        )
        self._timing_ran = LastValueGauge(
            meter,
            "defensive_front_timing_guard_ran",
            description=(
                "1 when the timing guard ran this pass, 0 when it could not. "
                "A guard that stopped running must not look like one that "
                "keeps passing."
            ),
        )
        self._degraded_upstreams = LastValueGauge(
            meter,
            "defensive_front_degraded_upstreams",
            description=(
                "How many optional feeds were unavailable on the last pass. "
                "Neither costs coverage, so nothing else would show it."
            ),
        )
        self._timing_flagged = meter.create_counter(
            "defensive_front_timing_confound_flagged_total",
            description=(
                "Passes whose adjusted pressure rate still tracked release "
                "timing -- the spec's failure mode, detected."
            ),
        )
        self._absences_unresolved = meter.create_counter(
            "defensive_front_absences_unresolved_total",
            description=(
                "Absent front starters player-identity did not resolve, so "
                "key_absences is short by a countable amount rather than "
                "quietly."
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

    def timing_guard(
        self,
        *,
        ran: bool,
        slope: float = NOT_RUN,
        t_statistic: float = NOT_RUN,
        flagged: bool = False,
    ) -> None:
        """The guard's behaviour, recorded whether or not it fired.

        Recorded on every pass including the ones that pass, because the
        interesting regression is not "it fired" but "it stopped being able
        to".
        """
        attributes = {"collector": self.collector}
        self._timing_ran.set(1.0 if ran else 0.0, attributes)
        self._timing_slope.set(slope, attributes)
        self._timing_t.set(t_statistic, attributes)
        if flagged:
            self._timing_flagged.add(1, attributes)

    def degraded_upstreams(self, count: int) -> None:
        self._degraded_upstreams.set(count, {"collector": self.collector})

    def absences_unresolved(self, count: int) -> None:
        if count:
            self._absences_unresolved.add(count, {"collector": self.collector})


metrics = DefensiveFrontMetrics()

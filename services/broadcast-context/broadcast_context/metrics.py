"""broadcast-context's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`, `collector_staleness_
seconds`) belong to the library, and the series that answer "is THIS collector
wrong in the way only it can be wrong" belong here.

The four series below exist because `collector_coverage_ratio` cannot see any
of them:

* **`unassigned_windows`** — games the feed lists with no kickoff time. These
  ARE coverage misses, so the ratio moves; the gauge separates "the feed has
  not slotted these yet", which is normal in December, from a genuine capture
  problem that moves the ratio the same way.
* **`incomplete_slate_weeks`** — weeks whose `games_in_window` was withheld.
  Coverage is untouched by this (the rows are still present), so without the
  gauge a fetch that silently lost half a week's games is invisible in
  Prometheus.
* **`flexed_games`** — games whose window has changed since we first saw it.
  This is the collector's actual product. A season that never leaves zero
  means either a quiet year or a broken history read, and the next gauge is
  what tells them apart.
* **`unevidenced_flex_claims`** — rows refused because they claimed a flex
  history the lake cannot evidence. Should be flat zero forever; anything else
  is a derivation bug, and it is the one failure that would otherwise write a
  fabricated history into an append-only lake.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a
capture cadence is absent from every scrape that does not immediately follow
one — and PromQL cannot tell an absent series from a healthy idle one. This
collector captures once a day, so a synchronous gauge would be missing from
roughly 5,759 of every 5,760 scrapes.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "broadcast-context"


class BroadcastContextMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "broadcast_context_rows_captured",
            description="Rows emitted in the last pass, by collector.",
        )
        self._unassigned_windows = LastValueGauge(
            meter,
            "broadcast_context_unassigned_windows",
            description=(
                "Games in the last pass with no broadcast window assigned, by "
                "collector. Never defaulted to sun_early."
            ),
        )
        self._incomplete_slate_weeks = LastValueGauge(
            meter,
            "broadcast_context_incomplete_slate_weeks",
            description=(
                "Weeks in the last pass whose games_in_window was withheld "
                "because the slate could not be shown complete, by collector."
            ),
        )
        self._flexed_games = LastValueGauge(
            meter,
            "broadcast_context_flexed_games",
            description=(
                "Games in the last pass whose broadcast window has changed "
                "since first observed, by collector."
            ),
        )
        self._unevidenced_flex_claims = LastValueGauge(
            meter,
            "broadcast_context_unevidenced_flex_claims",
            description=(
                "Rows refused in the last pass for claiming a flex history "
                "the lake cannot evidence, by collector. Expected zero."
            ),
        )

    def _set(self, gauge: LastValueGauge, count: int) -> None:
        gauge.set(count, {"collector": self.collector})

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable in
        PromQL, so a gauge only written when it is interesting cannot be
        alerted on. `LastValueGauge` is the other half of that: it keeps the
        series present on every scrape, not only the one after this call.
        """
        self._set(self._rows_captured, count)

    def unassigned_windows(self, count: int) -> None:
        self._set(self._unassigned_windows, count)

    def incomplete_slate_weeks(self, count: int) -> None:
        self._set(self._incomplete_slate_weeks, count)

    def flexed_games(self, count: int) -> None:
        self._set(self._flexed_games, count)

    def unevidenced_flex_claims(self, count: int) -> None:
        self._set(self._unevidenced_flex_claims, count)


metrics = BroadcastContextMetrics()

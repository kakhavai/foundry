"""player-contract's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`, `collector_staleness_
seconds`) belong to the library, and the series that answer "is THIS collector
wrong in the way only it can be wrong" belong here.

Both series below exist because `collector_coverage_ratio` structurally cannot
see the failure they describe:

* **`player_contract_expired_records`** — a record whose `contract_end_season`
  already passed is a *present* record with every field populated, so coverage
  reads 1.0 for it. This is the gauge that would have shown, on day one, that
  the `.csv.gz` upstream has not been regenerated since 2022-05-29 while the
  release around it is refreshed daily. It is the collector's staleness alarm,
  and nothing shared can raise it: only this collector knows that a contract has
  an expiry and that the capture season is supposed to fall inside it.

* **`player_contract_unresolved_scope_slots`** — the count of scoped players no
  resolved contract row covered. Coverage names them individually in `missing`,
  which is right for an operator reading one envelope and useless for an alert:
  `missing` is capped at 50 entries and is not a number PromQL can threshold.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a capture
cadence is absent from every scrape that does not immediately follow one — and
PromQL cannot tell an absent series from a healthy idle one. Counters are fine
as they are; only gauges need the wrapper.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "player-contract"


class PlayerContractMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._expired_records = LastValueGauge(
            meter,
            "player_contract_expired_records",
            description=(
                "Published records whose contract_end_season precedes the "
                "capture season, by collector. Non-zero means the upstream "
                "document predates the season being captured."
            ),
        )
        self._unresolved_scope_slots = LastValueGauge(
            meter,
            "player_contract_unresolved_scope_slots",
            description=(
                "Scoped players no resolved active contract row covered, by collector."
            ),
        )

    def expired_contract_records(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable in
        PromQL, so a gauge only written when it is interesting cannot be alerted
        on. `LastValueGauge` is the other half of that: it keeps the series
        present on every scrape, not only the one after this call.
        """
        self._expired_records.set(count, {"collector": self.collector})

    def unresolved_scope_slots(self, count: int) -> None:
        """Record every pass, including zero. Same reasoning as above."""
        self._unresolved_scope_slots.set(count, {"collector": self.collector})


metrics = PlayerContractMetrics()

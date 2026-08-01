"""officiating's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)`: the fleet-wide
series (`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that
answer "is THIS collector wrong in the way only it can be wrong" belong here.

This collector has three such ways, and `collector_coverage_ratio` can see none
of them — coverage counts rows published, and every failure below publishes a
row that looks perfectly well-formed:

`officiating_rates_refused`
    Rates withheld because their split-half correlation was indistinguishable
    from zero and they were not shrunk. Zero is the normal reading. A jump
    means an upstream change made the estimates look confidently different
    while correlating with nothing.

`officiating_low_continuity_assignments`
    The spec's own alarm: assignments below 0.6 continuity **whose crew's
    rates are being served**. Rates describing people who are not working the
    game. Deliberately not the raw count of low-continuity assignments — a
    crew whose rates are all refused is not making a false claim about anyone.

`officiating_referee_disagreements`
    Games where the schedule feed and the officials feed name different
    referees, compared on surname. Against 2025 this is 0 with the surname
    comparison and 17 without it (one man, "Ron" vs "Ronald"), so the series
    exists to catch the case the loose comparison cannot explain away: a
    crosswalk that has started joining the wrong games together.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a
capture cadence is absent from every scrape that does not immediately follow
one — and PromQL cannot tell an absent series from a healthy idle one.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "officiating"


class OfficiatingMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rates_refused = LastValueGauge(
            meter,
            "officiating_rates_refused",
            description=(
                "Crew rates withheld in the last pass because their split-half "
                "correlation was indistinguishable from zero and the estimate "
                "was not shrunk."
            ),
        )
        self._low_continuity = LastValueGauge(
            meter,
            "officiating_low_continuity_assignments",
            description=(
                "Assignments in the last pass whose crew continuity fell below "
                "0.6 while that crew's rates were being served."
            ),
        )
        self._referee_disagreements = LastValueGauge(
            meter,
            "officiating_referee_disagreements",
            description=(
                "Games in the last pass where the schedule feed and the "
                "officials feed named different referees."
            ),
        )
        self._crews_sampled = LastValueGauge(
            meter,
            "officiating_crews_sampled",
            description="Distinct crews with a rate window in the last pass.",
        )

    def rates_refused(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable in
        PromQL, so a gauge only written when it is interesting cannot be
        alerted on. `LastValueGauge` is the other half: it keeps the series
        present on every scrape, not only the one after this call.
        """
        self._rates_refused.set(count, {"collector": self.collector})

    def low_continuity_assignments(self, count: int) -> None:
        self._low_continuity.set(count, {"collector": self.collector})

    def referee_disagreements(self, count: int) -> None:
        self._referee_disagreements.set(count, {"collector": self.collector})

    def crews_sampled(self, count: int) -> None:
        self._crews_sampled.set(count, {"collector": self.collector})


metrics = OfficiatingMetrics()

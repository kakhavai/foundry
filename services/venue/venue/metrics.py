"""venue's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)`, and deliberately
**not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that answer
"is THIS collector wrong in the way only it can be wrong" belong here.

The three gauges below are that question for `venue`, and they exist because
`collector_coverage_ratio` **cannot** see this collector's real failure mode.
Coverage answers "did every game get an assignment". It answers `1.0` for a
season in which every game was assigned to a venue revision that was not yet
true — the fiction the phase-8 spec describes, where a Week 2 game is
attributed a surface installed in Week 11. Nothing about that is a coverage
miss; every row is present.

    venue_revision_window_misses    Games whose kickoff fell outside the window
                                    of the revision they resolved to. The
                                    direct read-time-join alarm the spec asks
                                    for. Any non-zero value is a real problem.

    venue_single_revision_venues    Venues in this pass carrying exactly one
                                    revision. The spec's second, subtler tell:
                                    "a venue known to have changed surfaces
                                    showing a single revision". It equals the
                                    venue count today, because the committed
                                    table carries no dated surface change yet —
                                    publishing that as a number rather than a
                                    footnote is the point. Once changes are
                                    sourced, a count that stops falling means
                                    somebody is overwriting in place.

    venue_unresolved_venues         Games whose venue could not be resolved at
                                    all — an unrecognised neutral-site stadium
                                    name. Separate from the window misses
                                    because the fix is different: one is a
                                    missing name spelling, the other is a
                                    missing or wrongly dated revision.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a capture
cadence is absent from every scrape that does not immediately follow one — and
PromQL cannot tell an absent series from a healthy idle one. That matters more
here than anywhere else in the fleet: this collector's cadence is
`static reference`, one pass a day against a 15-second scrape, so a synchronous
gauge would be visible on roughly one scrape in 5,000. Counters are already
cumulative and need none of this.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "venue"


class VenueMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "venue_rows_captured",
            description="Rows captured in the last pass, by signal type.",
        )
        self._revision_window_misses = LastValueGauge(
            meter,
            "venue_revision_window_misses",
            description=(
                "Games whose kickoff date fell outside the validity window of "
                "the venue revision they resolved to, in the last pass."
            ),
        )
        self._single_revision_venues = LastValueGauge(
            meter,
            "venue_single_revision_venues",
            description=(
                "Venues in the last pass carrying exactly one revision. A "
                "venue known to have changed surfaces showing one revision "
                "means the adapter is overwriting in place."
            ),
        )
        self._unresolved_venues = LastValueGauge(
            meter,
            "venue_unresolved_venues",
            description=(
                "Games whose venue could not be resolved from the feed's "
                "stadium name, in the last pass."
            ),
        )

    def rows_captured(self, signal_type: str, count: int) -> None:
        """Record every pass, including zero.

        Labelled by `signal_type` as well as `collector`: this collector's two
        types come from two different upstreams and one can be empty while the
        other is full, which a single unlabelled series would hide. The label
        set stays bounded — two values, fixed per process — which is what
        `LastValueGauge` requires.
        """
        self._rows_captured.set(
            count, {"collector": self.collector, "signal_type": signal_type}
        )

    def revision_window_misses(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable, so
        a gauge written only when it is interesting cannot be alerted on — and
        this is the one that alerts.
        """
        self._revision_window_misses.set(count, {"collector": self.collector})

    def single_revision_venues(self, count: int) -> None:
        """Record every pass, including zero."""
        self._single_revision_venues.set(count, {"collector": self.collector})

    def unresolved_venues(self, count: int) -> None:
        """Record every pass, including zero."""
        self._unresolved_venues.set(count, {"collector": self.collector})


metrics = VenueMetrics()

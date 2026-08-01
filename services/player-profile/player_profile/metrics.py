"""player-profile's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)`: the fleet-wide
series (`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that answer
"is THIS collector wrong in the way only it can be wrong" belong here.

The one that earns the subclass is `player_profile_stale_position_players`.
`collector_coverage_ratio` cannot see the spec's named failure mode at all: a
player whose listed position went stale two months ago still produces a
complete, well-formed, fully-covered record. Coverage reads 1.0 and the model
quietly uses the wrong age curve. This gauge is the only thing in the process
that can be alerted on for it.

**Every gauge is a `LastValueGauge`, never `meter.create_gauge`.** OTel's
synchronous gauge is *consumed* by a collection, so a value written on a capture
cadence is absent from every scrape that does not immediately follow one — and
PromQL cannot tell an absent series from a healthy idle one. On a
`static reference` cadence, where a pass runs once a day and Prometheus scrapes
every fifteen seconds, that would mean roughly one scrape in 5,760 carried a
value. Counters need none of this.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "player-profile"


class PlayerProfileMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "player_profile_rows_captured",
            description="Profile rows captured in the last pass, by collector.",
        )
        self._stale_position_players = LastValueGauge(
            meter,
            "player_profile_stale_position_players",
            description=(
                "Scoped players whose listed position has not been re-confirmed "
                "by any adapter within the staleness threshold, in season."
            ),
        )
        self._unresolved_scope_slots = LastValueGauge(
            meter,
            "player_profile_unresolved_scope_slots",
            description=(
                "Scope members no resolved upstream row covered in the last pass."
            ),
        )

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero."""
        self._rows_captured.set(count, {"collector": self.collector})

    def stale_position_players(self, count: int) -> None:
        """The spec's named failure mode, counted.

        Recorded on every pass including zero — which is the whole point. A
        gauge written only when it is interesting cannot distinguish "no stale
        positions" from "this collector stopped running", and those are exactly
        the two states an operator needs to tell apart here.
        """
        self._stale_position_players.set(count, {"collector": self.collector})

    def unresolved_scope_slots(self, count: int) -> None:
        """Watchlist players no upstream row covered. Record every pass."""
        self._unresolved_scope_slots.set(count, {"collector": self.collector})


metrics = PlayerProfileMetrics()

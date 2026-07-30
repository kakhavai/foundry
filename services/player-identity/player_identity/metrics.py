"""player-identity's binding to the shared collector metrics, plus the three
guards the Phase 8 spec calls for by name.

They are additions on a subclass rather than in `collector-core` on purpose:
`identity_resolution_failures_total` and friends are this collector's
concern, and the shared library must not grow a metric only one service
records. `IdentityMetrics` is still a `CollectorMetrics`, so it satisfies
`CollectorDescriptor.metrics` unchanged.
"""

from collector_core.metrics import CollectorMetrics
from opentelemetry import metrics as otel_metrics

COLLECTOR = "player-identity"


class IdentityMetrics(CollectorMetrics):
    def __init__(self, collector: str) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)

        # Labelled by *which attribute disagreed*, not just by collector.
        # That label is the whole point: a spike concentrated on `team` is a
        # staleness problem — a trade the book has not caught up with — and
        # reads completely differently from a spike spread evenly across
        # attributes, which is a genuine unknown. One unlabelled counter
        # cannot tell those two apart, and they need opposite responses.
        self._resolution_failures = meter.create_counter(
            "identity_resolution_failures",
            description=(
                "Resolution attempts that did not link, by the attribute that "
                "disagreed and the reason the link was refused."
            ),
        )
        # The injectivity invariant: each external_ids.<source>.id maps to
        # exactly one player_id per season. Anything above zero is a bad
        # merge, not a curiosity.
        self._merge_conflicts = meter.create_counter(
            "identity_merge_conflicts",
            description=("External ids claimed by more than one player_id, by source."),
        )
        # Near-miss density: rows above the agreement threshold but inside
        # the margin. These are ties sent to the queue deliberately, so a
        # rising rate means the weights need tuning — not that the upstream
        # got worse.
        self._near_misses = meter.create_counter(
            "identity_near_misses",
            description=(
                "Resolutions above the threshold but inside the margin, queued as ties."
            ),
        )

    def resolution_failure(self, attribute: str, reason: str) -> None:
        self._resolution_failures.add(
            1,
            {"collector": self.collector, "attribute": attribute, "reason": reason},
        )

    def merge_conflict(self, source: str) -> None:
        self._merge_conflicts.add(1, {"collector": self.collector, "source": source})

    def near_miss(self) -> None:
        self._near_misses.add(1, {"collector": self.collector})


metrics = IdentityMetrics(COLLECTOR)

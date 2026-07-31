"""usage-share's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`, `collector_staleness_
seconds`) belong to the library, and the series that answer "is THIS collector
wrong in the way only it can be wrong" belong here. A metric only one service
records must not grow into the shared library — see player-identity's
`identity_merge_conflicts` and roster-scope's `scope_missed_producers` for what
that looks like when it is real.

**The failure mode this collector has and nobody else does:** shares computed
against the wrong denominator produce numbers that look entirely normal. A 0.71
snap share is plausible whether the base was 62 real offensive snaps or 68
including kneels and special teams, and `collector_coverage_ratio` is blind to
it — every row is present, every field is populated, and the whole week is
quietly off by two to three points, which is enough to reorder a depth chart.

The spec's own tell is aggregate rather than per-row, and both halves of it are
recorded here:

- `usage_share_team_sum_drift` — the largest distance from 1.0 that any team's
  target shares summed to. Measured against the **upstream's own** share column,
  not this collector's, precisely so it is an independent quantity: shares this
  collector computes are divided by a denominator it summed itself, so they add
  to 1.0 by construction and would make the check vacuous.
- `usage_share_invalid_shares` — rows refused because a share fell outside
  [0, 1]. `snap_share` exceeding 1.0 is the spec's named impossible value.

`UsageShareMetrics` is still a `CollectorMetrics`, so it satisfies
`CollectorDescriptor.metrics` unchanged. Exactly one instance exists per
process — the library never constructs one, it takes the one below.
"""

from collector_core.metrics import CollectorMetrics
from opentelemetry import metrics as otel_metrics

COLLECTOR = "usage-share"

# The band the spec declares a team's summed target shares must land in. Outside
# it, the upstream's rows and its own denominator disagree.
TEAM_SUM_TOLERANCE = 0.03


class UsageShareMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = meter.create_gauge(
            "usage_share_rows_captured",
            description="Player usage rows captured in the last pass.",
        )
        self._team_sum_drift = meter.create_gauge(
            "usage_share_team_sum_drift",
            description=(
                "Largest |sum(upstream target_share) - 1.0| across teams in the "
                "last pass. Above 0.03 the upstream's rows disagree with its own "
                "denominator, and every share for that team is wrong by a margin "
                "no per-row check can see."
            ),
        )
        self._invalid_shares = meter.create_counter(
            "usage_share_invalid_shares",
            description=(
                "Rows refused because a computed share fell outside [0, 1] — the "
                "spec's impossible-value guard, e.g. snap_share above 1.0."
            ),
        )

    def rows_captured(self, count: int) -> None:
        """Record every pass, including zero.

        An absent Prometheus series and a healthy one are indistinguishable in
        PromQL, so a gauge only written when it is interesting cannot be
        alerted on.
        """
        self._rows_captured.set(count, {"collector": self.collector})

    def team_sum_drift(self, drift: float) -> None:
        """Record every pass, including a clean 0.0 — for the same reason."""
        self._team_sum_drift.set(drift, {"collector": self.collector})

    def invalid_share(self, field: str) -> None:
        """Labelled by *which* share was impossible: a spike concentrated on
        `snap_share` is a denominator problem and reads completely differently
        from one spread across every share."""
        self._invalid_shares.add(1, {"collector": self.collector, "field": field})


metrics = UsageShareMetrics()

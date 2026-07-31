"""roster-transactions's binding to the shared collector metrics.

A subclass rather than a bare `CollectorMetrics(COLLECTOR)` instance, and
deliberately **not** consolidated into `collector-core`: the fleet-wide series
(`collector_capture_*`, `collector_coverage_ratio`,
`collector_staleness_seconds`) belong to the library, and the series that answer
"is THIS collector wrong in the way only it can be wrong" belong here.

All three below exist because `collector_coverage_ratio` structurally cannot see
them. Coverage here counts *polling windows*, not transactions (see
`windows.py`), so a pass that covered every interval of the week and captured
absolutely nothing reports a perfect 1.0 — correctly, because a quiet week is a
real thing. That is the right coverage semantics and it leaves three blind
spots, one series each:

- `roster_transactions_rows_captured` — a quiet week and a feed that silently
  went empty look identical in the coverage block. They do not look identical
  here.
- `roster_transactions_unknown_types` — the closed vocabulary being breached is
  a rejected row, not a failed fetch, so `collector_capture_failures_total`
  never moves. A vendor renaming `ps_elevation` would otherwise be silent.
- `roster_transactions_duplicate_signings` — the phase doc's named failure
  mode: the same move arriving once as `reported` and once as `official` with a
  different `effective_at`, so the generator counts two signings where one
  occurred. Nothing errors and coverage is perfect; only reconciliation sees it.

Every one is recorded on **every** pass, including zero. An absent Prometheus
series and a healthy one are indistinguishable in PromQL, so a gauge written
only when it is interesting cannot be alerted on.

`RosterTransactionsMetrics` is still a `CollectorMetrics`, so it satisfies
`CollectorDescriptor.metrics` unchanged. Exactly one instance exists per
process — the library never constructs one, it takes the one below.
"""

from collector_core.metrics import CollectorMetrics, LastValueGauge
from opentelemetry import metrics as otel_metrics

COLLECTOR = "roster-transactions"


class RosterTransactionsMetrics(CollectorMetrics):
    def __init__(self, collector: str = COLLECTOR) -> None:
        super().__init__(collector)
        meter = otel_metrics.get_meter(collector)
        self._rows_captured = LastValueGauge(
            meter,
            "roster_transactions_rows_captured",
            description="Transaction rows captured in the last pass, by collector.",
        )
        self._unknown_types = LastValueGauge(
            meter,
            "roster_transactions_unknown_types",
            description=(
                "Rows rejected in the last pass for a transaction_type outside "
                "the closed vocabulary, by collector."
            ),
        )
        self._duplicate_signings = LastValueGauge(
            meter,
            "roster_transactions_duplicate_signings",
            description=(
                "(player_id, to_team) pairs with two signing-class rows inside "
                "72 hours and no intervening departure, by collector."
            ),
        )

    def rows_captured(self, count: int) -> None:
        self._rows_captured.set(count, {"collector": self.collector})

    def unknown_transaction_types(self, count: int) -> None:
        self._unknown_types.set(count, {"collector": self.collector})

    def duplicate_signings(self, count: int) -> None:
        self._duplicate_signings.set(count, {"collector": self.collector})


metrics = RosterTransactionsMetrics()

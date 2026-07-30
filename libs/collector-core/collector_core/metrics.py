"""Fleet-wide capture metrics, shared by every collector.

`/weather/stadiums`-style routes can deliberately degrade a failed item to
`None` and still return 200 with the full count, so the HTTP response alone
cannot reveal partial or total upstream failure. These counters can.

Names follow the Phase 8 collector-fleet convention — every collector reports
`collector_capture_*` carrying a `collector` label, so one Prometheus query
spans the fleet instead of twenty-six service-specific series. See
docs/architecture/phase-8-data-source-collectors.md. `player-projections` is
deliberately excluded: it consumes the generator's output rather than capturing
a signal, so it is not a collector and keeps its `upstream_*` names.
"""

import httpx
from opentelemetry import metrics


class CollectorMetrics:
    """Per-collector metric recorder. One instance per collector process,
    constructed with that collector's name so the `collector` label is
    carried automatically on every recording."""

    def __init__(self, collector: str) -> None:
        self.collector = collector
        meter = metrics.get_meter(collector)

        # OTel appends `_total`, so these render as
        # `collector_capture_requests_total` and
        # `collector_capture_failures_total`. Two counters rather than one
        # with an `outcome` label, so the failure ratio is
        # failures/requests without summing across label values.
        self._requests = meter.create_counter(
            "collector_capture_requests",
            description="Upstream capture calls attempted, by collector.",
        )
        self._failures = meter.create_counter(
            "collector_capture_failures",
            description="Upstream capture calls that failed, by collector and cause.",
        )
        self._auth_failures = meter.create_counter(
            "collector_auth_failures",
            description="Collector API requests rejected by the token check, by cause.",
        )
        self._coverage_ratio = meter.create_gauge(
            "collector_coverage_ratio",
            description=(
                "present/expected for the last capture, by collector and signal type."
            ),
        )
        self._staleness = meter.create_gauge(
            "collector_staleness_seconds",
            description="Seconds since the last successful capture, by collector.",
        )

    @staticmethod
    def _reason(exc: BaseException) -> str:
        """Classify an upstream failure for the `reason` label.

        Order matters: `httpx.TimeoutException` subclasses `RequestError`, so it
        must be tested first or every timeout is mislabelled `transport`.
        """
        if isinstance(exc, httpx.HTTPStatusError):
            return "http_status"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.RequestError):
            return "transport"
        if isinstance(exc, (KeyError, TypeError, ValueError)):
            return "malformed"
        return "unknown"

    @staticmethod
    def reason_for(exc: BaseException) -> str:
        """Public alias for the failure classifier, used by capture orchestration."""
        return CollectorMetrics._reason(exc)

    def capture_attempt(self) -> None:
        self._requests.add(1, {"collector": self.collector})

    def capture_failure(self, exc: BaseException) -> None:
        self._failures.add(
            1, {"collector": self.collector, "reason": self._reason(exc)}
        )

    def auth_failure(self, reason: str) -> None:
        """A rejection must be observable — `unconfigured` in particular, since
        it means the Secret never arrived and every caller is being turned
        away."""
        self._auth_failures.add(1, {"collector": self.collector, "reason": reason})

    def coverage(self, signal_type: str, ratio: float) -> None:
        self._coverage_ratio.set(
            ratio, {"collector": self.collector, "signal_type": signal_type}
        )

    def staleness(self, seconds: float) -> None:
        self._staleness.set(seconds, {"collector": self.collector})

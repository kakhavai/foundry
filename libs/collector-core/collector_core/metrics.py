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

**Every gauge here is observable, and that is not a style choice.** See
`LastValueGauge`.
"""

import threading
from collections.abc import Mapping, Sequence

import httpx
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation


class LastValueGauge:
    """A gauge whose value survives a scrape it was not written before.

    Drop-in for `meter.create_gauge(...)` — same `.set(value, attributes)` call
    shape — and every collector gauge in the fleet must use it instead.

    OTel's **synchronous** gauge is last-value aggregated with the value
    *consumed* by a collection: `_LastValueAggregation.collect` returns the
    point once and clears it, so the series appears on the first scrape after a
    recording and is **absent from every scrape after that**. Collectors record
    on a capture cadence — minutes to hours — while Prometheus scrapes every
    15-30 seconds, so the overwhelming majority of scrapes see nothing at all.

    That is worse than a wrong number. `collector_coverage_ratio` exists so a
    collector silently capturing 3% of the league is *visible*, and PromQL
    cannot tell a series that never existed from one that is healthy and idle —
    which is exactly why `scripts/run-chaos.py` treats an empty query result as
    a hard error rather than a zero. A chaos criterion or an alert on a
    synchronous gauge is flaky by construction.

    An **observable** gauge inverts the ownership: the callback runs at
    collection time and reports current state, so every scrape carries every
    label set that has ever been written. `set` becomes a dict write.

    Two properties the callback must keep, both load-bearing:

    - **It must never block.** It runs on whichever thread drives the
      collection, which for `/metrics` is the event loop thread. It touches a
      dict under a lock held for a copy and nothing else — no I/O, no `await`,
      and above all nothing that reaches the lake, since
      `EventLoopGuardedLake` *raises* on a loop-thread call.
    - **A label set, once written, is reported forever.** That is the point,
      and it is also the cost: this is only safe for bounded label sets. Every
      gauge in the fleet is keyed by `collector` and at most `signal_type`,
      both of which are fixed per process.

    One limitation it does *not* fix: `collector_staleness_seconds` is still
    the value the capture loop last wrote, not seconds-since-capture recomputed
    at scrape time, so it steps at the loop's cadence rather than climbing
    smoothly. Present and slightly coarse beats absent; making it continuous
    would mean handing the clock and `CaptureState` to the recorder.
    """

    def __init__(
        self,
        meter: metrics.Meter,
        name: str,
        *,
        description: str = "",
        unit: str = "",
    ) -> None:
        self._values: dict[tuple[tuple[str, object], ...], float] = {}
        # Guards a dict copy, never I/O. The GIL alone would very nearly do,
        # but "very nearly" in a callback that runs on the event loop is not a
        # thing worth being clever about.
        self._lock = threading.Lock()
        self._gauge = meter.create_observable_gauge(
            name,
            callbacks=[self._observe],
            description=description,
            unit=unit,
        )

    def _observe(self, options: CallbackOptions) -> Sequence[Observation]:
        with self._lock:
            snapshot = list(self._values.items())
        return [Observation(value, dict(key)) for key, value in snapshot]

    def set(self, value: float, attributes: Mapping[str, object] | None = None) -> None:
        """Replace this label set's value. Read back on the next scrape."""
        key = tuple(sorted((attributes or {}).items()))
        with self._lock:
            self._values[key] = float(value)


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
        # Observable, not synchronous. A synchronous gauge here is absent from
        # every scrape that does not immediately follow a capture -- see
        # `LastValueGauge`.
        self._coverage_ratio = LastValueGauge(
            meter,
            "collector_coverage_ratio",
            description=(
                "present/expected for the last capture, by collector and signal type."
            ),
        )
        self._staleness = LastValueGauge(
            meter,
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

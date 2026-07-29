"""Capture metrics for the Open-Meteo calls.

`/weather/stadiums` deliberately degrades a failed stadium to `weather: None`
and still returns 200 with the full count, so the HTTP response cannot reveal
partial or total upstream failure. These counters can.

Names follow the Phase 8 collector-fleet convention — every collector reports
`collector_capture_*` carrying a `collector` label, so one Prometheus query
spans the fleet instead of twenty-six service-specific series. See
docs/architecture/phase-8-data-source-collectors.md. `player-projections` is
deliberately excluded: it consumes the generator's output rather than capturing
a signal, so it is not a collector and keeps its `upstream_*` names.
"""

import httpx
from opentelemetry import metrics

COLLECTOR = "weather"

_meter = metrics.get_meter("weather")

# OTel appends `_total`, so these render as `collector_capture_requests_total`
# and `collector_capture_failures_total`. Two counters rather than one with an
# `outcome` label, so the failure ratio is failures/requests without summing
# across label values.
_requests = _meter.create_counter(
    "collector_capture_requests",
    description="Upstream capture calls attempted, by collector.",
)
_failures = _meter.create_counter(
    "collector_capture_failures",
    description="Upstream capture calls that failed, by collector and cause.",
)


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


def record_upstream_attempt() -> None:
    _requests.add(1, {"collector": COLLECTOR})


def record_upstream_failure(exc: BaseException) -> None:
    _failures.add(1, {"collector": COLLECTOR, "reason": _reason(exc)})

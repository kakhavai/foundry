"""Upstream failure metrics for the Open-Meteo calls.

`/weather/stadiums` deliberately degrades a failed stadium to `weather: None`
and still returns 200 with the full count, so the HTTP response cannot reveal
partial or total upstream failure. These counters can.
"""

import httpx
from opentelemetry import metrics

_meter = metrics.get_meter("weather")

# OTel appends `_total`, so these render as `weather_upstream_requests_total`
# and `weather_upstream_failures_total`. Two counters rather than one with an
# `outcome` label, so the failure ratio is failures/requests without summing
# across label values.
_requests = _meter.create_counter(
    "weather_upstream_requests",
    description="Upstream weather API calls attempted.",
)
_failures = _meter.create_counter(
    "weather_upstream_failures",
    description="Upstream weather API calls that failed, by cause.",
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
    _requests.add(1)


def record_upstream_failure(exc: BaseException) -> None:
    _failures.add(1, {"reason": _reason(exc)})

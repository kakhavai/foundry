"""Failure-path metrics for the projections poll loop.

Instruments are created at import; the MeterProvider is installed later by
`lifespan` (production) or by the session fixture in conftest (tests). This
module must not import `.main` — `.main` imports this one.
"""

import time

import httpx
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from .client import MalformedSnapshotError

_meter = metrics.get_meter("player_projections")

# OTel appends `_total` to counters and derives `_seconds` from `unit="s"`, so
# these render as `upstream_poll_failures_total` and `upstream_cache_age_seconds`.
_poll_failures = _meter.create_counter(
    "upstream_poll_failures",
    description="Failed upstream projection polls, by scoring format and cause.",
)

# Gauge state. `_healthy` carries every known format from startup so the series
# always exists; `_last_success` stays empty until a format first succeeds,
# because a cache age of 0 would read as "just refreshed" — the opposite of true.
_last_success: dict[str, float] = {}
_healthy: dict[str, bool] = {}


def _reason(exc: BaseException) -> str:
    """Classify a poll failure for the `reason` label.

    Order matters: `httpx.TimeoutException` subclasses `RequestError`, so it must
    be tested first or every timeout is mislabelled `transport`.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "transport"
    if isinstance(exc, MalformedSnapshotError):
        return "malformed"
    return "unknown"


def register_format(fmt: str) -> None:
    """Seed the health gauge so it reports 0 from startup, stub mode included."""
    _healthy.setdefault(fmt, False)


def record_poll_success(fmt: str) -> None:
    _last_success[fmt] = time.monotonic()
    _healthy[fmt] = True


def record_poll_failure(fmt: str, exc: BaseException) -> None:
    _poll_failures.add(1, {"format": fmt, "reason": _reason(exc)})
    _healthy[fmt] = False


def _cache_age_callback(options: CallbackOptions):
    now = time.monotonic()
    for fmt, succeeded_at in _last_success.items():
        yield Observation(now - succeeded_at, {"format": fmt})


def _healthy_callback(options: CallbackOptions):
    for fmt, healthy in _healthy.items():
        yield Observation(1 if healthy else 0, {"format": fmt})


# Observable gauges run their callback at scrape time. That is what makes cache
# age correct with a 900s poll interval — a value written at poll time would be
# up to fifteen minutes stale by the time Prometheus read it.
_meter.create_observable_gauge(
    "upstream_cache_age",
    callbacks=[_cache_age_callback],
    unit="s",
    description="Seconds since this format last polled successfully.",
)
_meter.create_observable_gauge(
    "upstream_healthy",
    callbacks=[_healthy_callback],
    description="1 when the format's last poll succeeded, 0 otherwise.",
)

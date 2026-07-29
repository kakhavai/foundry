import asyncio

import httpx
import pytest

from player_projections import main
from player_projections import metrics as pp_metrics
from player_projections.client import MalformedSnapshotError

URL_TEMPLATE = "https://example.test/{format}.json"


@pytest.fixture(autouse=True)
def reset_state():
    """Both the cache and the gauge-backing dicts are module-global."""
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()
    pp_metrics._last_success.clear()
    pp_metrics._healthy.clear()
    for fmt in main.FORMATS:
        pp_metrics.register_format(fmt)
    yield
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()


@pytest.fixture
def one_iteration(monkeypatch):
    """Make the infinite poll loop run exactly one pass, then stop."""

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)


def _always_raise(exc: BaseException):
    async def _fetch(url, expect_format=None):
        raise exc

    return _fetch


def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/ppr.json")
    return httpx.HTTPStatusError(
        "500 Server Error",
        request=request,
        response=httpx.Response(500, request=request),
    )


@pytest.mark.parametrize(
    "make_exc, reason",
    [
        (_http_status_error, "http_status"),
        (lambda: httpx.ConnectTimeout("timed out"), "timeout"),
        (lambda: httpx.ConnectError("connection refused"), "transport"),
        (lambda: MalformedSnapshotError("not a JSON object"), "malformed"),
        (lambda: RuntimeError("something unforeseen"), "unknown"),
    ],
)
async def test_each_failure_class_increments_its_own_reason(
    monkeypatch, one_iteration, metric_value, make_exc, reason
):
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)
    monkeypatch.setattr(main, "fetch_projections", _always_raise(make_exc()))

    before = (
        metric_value("upstream_poll_failures_total", format="ppr", reason=reason) or 0.0
    )
    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()
    after = (
        metric_value("upstream_poll_failures_total", format="ppr", reason=reason) or 0.0
    )

    assert after - before == 1.0


async def test_a_failing_format_does_not_increment_another_format(
    monkeypatch, one_iteration, metric_value
):
    """The metric analogue of test_one_format_failing_does_not_affect_the_others."""
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)

    async def only_half_ppr_fails(url, expect_format=None):
        if expect_format == "half-ppr":
            raise MalformedSnapshotError("that document is corrupt")
        return []

    monkeypatch.setattr(main, "fetch_projections", only_half_ppr_fails)

    args = {"reason": "malformed"}
    before_bad = (
        metric_value("upstream_poll_failures_total", format="half-ppr", **args) or 0.0
    )
    before_good = (
        metric_value("upstream_poll_failures_total", format="ppr", **args) or 0.0
    )
    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()
    after_bad = (
        metric_value("upstream_poll_failures_total", format="half-ppr", **args) or 0.0
    )
    after_good = (
        metric_value("upstream_poll_failures_total", format="ppr", **args) or 0.0
    )

    assert after_bad - before_bad == 1.0
    assert after_good - before_good == 0.0

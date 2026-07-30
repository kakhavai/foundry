import httpx

from collector_core.metrics import CollectorMetrics


def test_reason_for_classifies_http_status():
    request = httpx.Request("GET", "https://example.invalid")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    assert CollectorMetrics.reason_for(exc) == "http_status"


def test_timeout_is_classified_before_transport():
    """TimeoutException subclasses RequestError. It must be tested first or
    every timeout is mislabelled `transport` and the two collapse into one
    bucket, hiding a rate-limited upstream behind a connectivity story."""
    assert CollectorMetrics.reason_for(httpx.TimeoutException("x")) == "timeout"


def test_reason_for_classifies_transport():
    assert CollectorMetrics.reason_for(httpx.ConnectError("x")) == "transport"


def test_reason_for_classifies_malformed():
    for exc in (KeyError("x"), TypeError("x"), ValueError("x")):
        assert CollectorMetrics.reason_for(exc) == "malformed"


def test_reason_for_falls_back_to_unknown():
    assert CollectorMetrics.reason_for(RuntimeError("x")) == "unknown"


def test_each_instance_carries_its_own_collector_label():
    assert CollectorMetrics("weather").collector == "weather"
    assert CollectorMetrics("betting-lines").collector == "betting-lines"


def test_recording_is_inert_without_a_meter_provider():
    """OTel is not initialised in tests. Recording must be a no-op, not raise —
    a service that crashes when unobserved is worse than an unobserved one."""
    m = CollectorMetrics("weather")
    m.capture_attempt()
    m.capture_failure(httpx.TimeoutException("x"))
    m.auth_failure("missing")
    m.coverage("venue_forecast_kickoff", 0.5)
    m.staleness(12.0)

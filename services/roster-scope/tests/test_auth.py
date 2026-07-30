"""Auth is enforced in the service, not at the gateway.

`scripts/smoke-test.sh` port-forwards the Service directly, so gateway-only
auth would leave the required `integration-test` check green over an
unprotected path. Middleware, so a route added later is protected by default —
which is exactly what `/scope/players` and friends are relying on.
"""

import pytest
from fastapi.testclient import TestClient

from roster_scope.main import app

PROTECTED = ["/signals", "/catalog", "/scope/players", "/scope/rules"]


def anonymous() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize("path", PROTECTED)
def test_every_data_route_needs_a_token(path):
    """Including the three routes this service adds *after*
    `build_collector_app` returns — the whole point of enforcing in
    middleware rather than per-route."""
    response = anonymous().get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_refresh_needs_a_token():
    assert anonymous().post("/refresh").status_code == 401


def test_missing_token_is_counted(metric_value):
    before = (
        metric_value(
            "collector_auth_failures_total",
            collector="roster-scope",
            reason="missing",
        )
        or 0.0
    )
    assert anonymous().get("/signals").status_code == 401
    after = metric_value(
        "collector_auth_failures_total", collector="roster-scope", reason="missing"
    )
    assert after - before == 1.0


@pytest.mark.parametrize(
    "header", ["Basic abc123", "token abc123", "Bearer", "Bearer ", "abc123"]
)
def test_malformed_authorization_header_is_rejected(header):
    assert (
        anonymous().get("/signals", headers={"Authorization": header}).status_code
        == 401
    )


def test_extra_spaces_between_scheme_and_token_are_accepted(collector_token):
    """RFC 7235 allows `1*SP` after the scheme."""
    response = anonymous().get(
        "/signals", headers={"Authorization": f"Bearer   {collector_token}"}
    )
    assert response.status_code == 200


def test_wrong_token_is_rejected():
    assert (
        anonymous()
        .get("/signals", headers={"Authorization": "Bearer nope"})
        .status_code
        == 401
    )


def test_rotation(monkeypatch, collector_token):
    """In a live pod the new value arrives only after a restart, because
    `secretKeyRef` injects it as an env var captured at pod start. The check
    reads the env per request, so swapping it here is the same event."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "rotated-token")
    assert (
        anonymous()
        .get("/signals", headers={"Authorization": f"Bearer {collector_token}"})
        .status_code
        == 401
    )
    assert (
        anonymous()
        .get("/signals", headers={"Authorization": "Bearer rotated-token"})
        .status_code
        == 200
    )


def test_unconfigured_token_fails_closed(monkeypatch):
    """`optional: true` on the secretKeyRef lets the pod start before its
    Secret exists. That has to mean "started and refusing", or a Secret that
    never syncs silently publishes an open collector."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    response = anonymous().get("/signals", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503


def test_health_and_metrics_need_no_token():
    """The kubelet's probes and Prometheus's annotation scrape cannot carry one."""
    anon = anonymous()
    assert anon.get("/health").status_code == 200
    assert anon.get("/metrics").status_code == 200


def test_health_stays_up_when_the_token_is_unconfigured(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    assert anonymous().get("/health").status_code == 200


def test_unknown_path_is_401_when_unauthenticated():
    """Middleware runs before routing, so it does not leak which routes exist."""
    assert anonymous().get("/no-such-route").status_code == 401

"""Auth is a new failure surface, so it gets its own rejection tests.

Enforcement lives in the service rather than at the gateway. The reason is
visible in `scripts/smoke-test.sh`: it port-forwards `svc/weather` directly, so
gateway-only auth would leave the required `integration-test` check green over
an unprotected path.

`/signals` stands in for "a protected data route" throughout — it replaced
`/weather/stadiums` in Task 13 and, unlike the old route, never calls an
upstream itself, so these tests need no respx mocking to reach 200.
"""

import pytest
from fastapi.testclient import TestClient

from weather.main import app

SIGNALS_PATH = "/signals"


def anonymous() -> TestClient:
    """A client carrying no Authorization header."""
    return TestClient(app)


def test_missing_token_is_rejected(metric_value):
    before = (
        metric_value(
            "collector_auth_failures_total", collector="weather", reason="missing"
        )
        or 0.0
    )
    response = anonymous().get(SIGNALS_PATH)
    after = (
        metric_value(
            "collector_auth_failures_total", collector="weather", reason="missing"
        )
        or 0.0
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert after - before == 1.0


@pytest.mark.parametrize(
    "header",
    [
        "Basic abc123",
        "token abc123",
        "Bearer",
        "Bearer ",
        "abc123",
    ],
)
def test_malformed_authorization_header_is_rejected(header):
    response = anonymous().get(SIGNALS_PATH, headers={"Authorization": header})

    assert response.status_code == 401


def test_extra_spaces_between_scheme_and_token_are_accepted(collector_token):
    """RFC 7235 allows `1*SP` after the scheme, so `Bearer  <token>` is valid.

    Rejecting it fails closed, so this was never a security problem — just a
    caller that cannot authenticate for a reason no error message explains.
    """
    response = anonymous().get(
        SIGNALS_PATH, headers={"Authorization": f"Bearer   {collector_token}"}
    )

    assert response.status_code == 200


def test_wrong_token_is_rejected(metric_value):
    before = (
        metric_value(
            "collector_auth_failures_total", collector="weather", reason="invalid"
        )
        or 0.0
    )
    response = anonymous().get(
        SIGNALS_PATH, headers={"Authorization": "Bearer not-the-real-token"}
    )
    after = (
        metric_value(
            "collector_auth_failures_total", collector="weather", reason="invalid"
        )
        or 0.0
    )

    assert response.status_code == 401
    assert after - before == 1.0


def test_superseded_token_is_rejected(monkeypatch, collector_token):
    """Rotation, in the only shape this process can observe it.

    In a live pod the new value arrives only after a restart, because
    `secretKeyRef` injects the token as an env var captured at pod start. The
    check itself reads the env per request, so swapping it here is the same
    event the pod sees after a rollout.
    """
    monkeypatch.setenv("COLLECTOR_TOKEN", "rotated-token")

    response = anonymous().get(
        SIGNALS_PATH, headers={"Authorization": f"Bearer {collector_token}"}
    )

    assert response.status_code == 401


def test_rotated_token_is_accepted(monkeypatch):
    """The other half of rotation: the new token works immediately."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "rotated-token")

    response = anonymous().get(
        SIGNALS_PATH, headers={"Authorization": "Bearer rotated-token"}
    )

    assert response.status_code == 200


def test_valid_token_reaches_the_route(client):
    response = client.get(SIGNALS_PATH)

    assert response.status_code == 200
    assert response.json() == {"envelopes": [], "count": 0}


def test_unconfigured_token_fails_closed(monkeypatch, metric_value):
    """An absent Secret must refuse traffic, not serve it unauthenticated.

    `optional: true` on the secretKeyRef lets the pod start before its Secret
    exists. That has to mean "started and refusing", or a Secret that fails to
    sync silently publishes an open collector.
    """
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    before = (
        metric_value(
            "collector_auth_failures_total", collector="weather", reason="unconfigured"
        )
        or 0.0
    )

    response = anonymous().get(
        SIGNALS_PATH, headers={"Authorization": "Bearer anything-at-all"}
    )

    after = (
        metric_value(
            "collector_auth_failures_total", collector="weather", reason="unconfigured"
        )
        or 0.0
    )
    assert response.status_code == 503
    assert after - before == 1.0


def test_health_and_metrics_need_no_token():
    """The kubelet's probes and Prometheus's annotation scrape cannot carry one."""
    anon = anonymous()

    assert anon.get("/health").status_code == 200
    assert anon.get("/metrics").status_code == 200


def test_health_stays_up_when_the_token_is_unconfigured(monkeypatch):
    """Otherwise a missing Secret becomes a crash loop instead of a loud 503."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "")

    assert anonymous().get("/health").status_code == 200


def test_unknown_path_is_401_when_unauthenticated():
    """Middleware runs before routing. Deliberate: it does not leak which
    routes exist."""
    assert anonymous().get("/no-such-route").status_code == 401

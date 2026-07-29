"""Auth is a new failure surface, so it gets its own rejection tests.

Enforcement lives in the service rather than at the gateway. The reason is
visible in `scripts/smoke-test.sh`: it port-forwards `svc/weather` directly, so
gateway-only auth would leave the required `integration-test` check green over
an unprotected path.
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from weather.client import WEATHER_URL
from weather.main import app

STADIUM_PATH = "/weather/stadiums/lambeau"

GOOD_BODY = {
    "current": {
        "time": "2026-07-28T12:00",
        "temperature_2m": 18.5,
        "relative_humidity_2m": 65,
        "wind_speed_10m": 12.3,
        "weather_code": 2,
        "precipitation": 0.0,
    }
}


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
    response = anonymous().get(STADIUM_PATH)
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
    response = anonymous().get(STADIUM_PATH, headers={"Authorization": header})

    assert response.status_code == 401


def test_wrong_token_is_rejected(metric_value):
    before = (
        metric_value(
            "collector_auth_failures_total", collector="weather", reason="invalid"
        )
        or 0.0
    )
    response = anonymous().get(
        STADIUM_PATH, headers={"Authorization": "Bearer not-the-real-token"}
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
        STADIUM_PATH, headers={"Authorization": f"Bearer {collector_token}"}
    )

    assert response.status_code == 401


@respx.mock
def test_rotated_token_is_accepted(monkeypatch):
    """The other half of rotation: the new token works immediately."""
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=GOOD_BODY))
    monkeypatch.setenv("COLLECTOR_TOKEN", "rotated-token")

    response = anonymous().get(
        STADIUM_PATH, headers={"Authorization": "Bearer rotated-token"}
    )

    assert response.status_code == 200


@respx.mock
def test_valid_token_reaches_the_route(client):
    respx.get(WEATHER_URL).mock(return_value=httpx.Response(200, json=GOOD_BODY))

    response = client.get(STADIUM_PATH)

    assert response.status_code == 200
    assert response.json()["id"] == "lambeau"


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
        STADIUM_PATH, headers={"Authorization": "Bearer anything-at-all"}
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

"""Bearer auth, mounted by `build_collector_app`.

The resolve routes are the reason this is worth re-asserting per service:
they are added *after* the `build_collector_app` call, and middleware is what
makes a route added later protected by default.
"""

import pytest
from fastapi.testclient import TestClient

from player_identity.main import app

PROTECTED = [
    "/catalog",
    "/signals",
    "/resolve?name=Someone",
    "/unresolved",
]


@pytest.fixture
def anonymous():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", PROTECTED)
def test_a_data_route_without_a_token_is_401(anonymous, path):
    assert anonymous.get(path).status_code == 401


def test_the_batch_route_without_a_token_is_401(anonymous):
    assert anonymous.post("/resolve/batch", json={"queries": []}).status_code == 401


@pytest.mark.parametrize("path", ["/health", "/metrics"])
def test_the_exempt_paths_answer_without_a_token(anonymous, path):
    assert anonymous.get(path).status_code == 200


def test_a_wrong_token_is_401(anonymous):
    assert (
        anonymous.get("/catalog", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


def test_an_absent_secret_fails_closed_with_503(monkeypatch):
    """An unconfigured collector must be loud, not open. The Secret is not
    managed by GitOps, so "never synced" is a real state."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    with TestClient(app) as anon:
        assert anon.get("/catalog").status_code == 503
        assert anon.get("/resolve?name=Someone").status_code == 503
        assert anon.get("/health").status_code == 200

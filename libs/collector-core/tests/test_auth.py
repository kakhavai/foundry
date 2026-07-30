import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collector_core.auth import DEFAULT_EXEMPT_PATHS, build_bearer_middleware
from collector_core.metrics import CollectorMetrics

TOKEN = "test-token"


def build_app(monkeypatch, token: str | None = TOKEN) -> FastAPI:
    if token is None:
        monkeypatch.delenv("COLLECTOR_TOKEN", raising=False)
    else:
        monkeypatch.setenv("COLLECTOR_TOKEN", token)
    app = FastAPI()
    app.middleware("http")(
        build_bearer_middleware(CollectorMetrics("weather"), DEFAULT_EXEMPT_PATHS)
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/signals")
    async def signals():
        return {"envelopes": []}

    return app


def test_exempt_path_needs_no_token(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        assert c.get("/health").status_code == 200


def test_missing_token_is_rejected(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        assert c.get("/signals").status_code == 401


def test_correct_token_is_accepted(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200


@pytest.mark.parametrize(
    "header", ["", "Bearer", "Bearer ", "Basic abc", TOKEN, "Bearer  "]
)
def test_malformed_authorization_header_is_rejected(monkeypatch, header):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": header})
        assert r.status_code == 401


def test_extra_spaces_between_scheme_and_token_are_tolerated(monkeypatch):
    """RFC 7235 permits more than one space. Folding the extras into the token
    would reject a well-formed header."""
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": f"Bearer   {TOKEN}"})
        assert r.status_code == 200


def test_wrong_token_is_rejected(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


def test_rejection_carries_the_www_authenticate_header(monkeypatch):
    with TestClient(build_app(monkeypatch)) as c:
        r = c.get("/signals")
        assert r.headers["WWW-Authenticate"] == "Bearer"


def test_unconfigured_token_fails_closed_with_503(monkeypatch):
    """An absent or empty secret must close the collector, never open it. A
    Secret that never syncs is then loud rather than an open data route."""
    with TestClient(build_app(monkeypatch, token=None)) as c:
        assert c.get("/signals").status_code == 503


def test_unconfigured_still_serves_exempt_paths(monkeypatch):
    """The kubelet probe and the metrics scrape cannot carry a token, so a
    missing secret must be a loud 503 on data routes rather than a crash loop
    with no metrics to explain it."""
    with TestClient(build_app(monkeypatch, token=None)) as c:
        assert c.get("/health").status_code == 200

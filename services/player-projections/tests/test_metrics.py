from fastapi.testclient import TestClient

from player_projections.main import app

client = TestClient(app)


def test_metrics_endpoint_returns_prometheus_format():
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"# HELP" in r.content

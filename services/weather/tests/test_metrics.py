from fastapi.testclient import TestClient

from weather.main import app

client = TestClient(app)


def test_metrics_endpoint_returns_prometheus_format():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

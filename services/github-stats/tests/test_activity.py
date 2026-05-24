import httpx
import respx
from fastapi.testclient import TestClient

from github_stats.main import app

client = TestClient(app)

MOCK_EVENTS = [
    {
        "type": "PushEvent",
        "repo": {"name": "testuser/repo-a"},
        "created_at": "2026-05-01T10:00:00Z",
        "payload": {"distinct_size": 3},
    },
    {
        "type": "PullRequestEvent",
        "repo": {"name": "testuser/repo-b"},
        "created_at": "2026-05-02T11:00:00Z",
        "payload": {},
    },
]


@respx.mock
def test_activity_returns_events_for_known_user():
    respx.get("https://api.github.com/users/testuser/events").mock(
        return_value=httpx.Response(200, json=MOCK_EVENTS)
    )
    response = client.get("/activity/testuser")
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert len(data["events"]) == 2
    assert data["events"][0]["type"] == "PushEvent"
    assert data["events"][0]["repo"] == "testuser/repo-a"
    assert data["events"][0]["created_at"] == "2026-05-01T10:00:00Z"


@respx.mock
def test_activity_returns_404_for_unknown_user():
    respx.get("https://api.github.com/users/nobody/events").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    response = client.get("/activity/nobody")
    assert response.status_code == 404


@respx.mock
def test_activity_caps_at_10_events():
    many_events = MOCK_EVENTS * 10  # 20 events
    respx.get("https://api.github.com/users/testuser/events").mock(
        return_value=httpx.Response(200, json=many_events)
    )
    response = client.get("/activity/testuser")
    assert response.status_code == 200
    assert len(response.json()["events"]) == 10

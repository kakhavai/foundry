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
        "type": "PushEvent",
        "repo": {"name": "testuser/repo-a"},
        "created_at": "2026-05-02T09:00:00Z",
        "payload": {"distinct_size": 2},
    },
    {
        "type": "PullRequestEvent",
        "repo": {"name": "testuser/repo-b"},
        "created_at": "2026-05-03T14:00:00Z",
        "payload": {},
    },
]


@respx.mock
def test_stats_returns_summary_for_known_user():
    respx.get("https://api.github.com/users/testuser/events").mock(
        return_value=httpx.Response(200, json=MOCK_EVENTS)
    )
    response = client.get("/stats/testuser")
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert data["commit_count"] == 5  # 3 + 2
    assert data["pr_count"] == 1
    assert "testuser/repo-a" in data["top_repos"]


@respx.mock
def test_stats_returns_404_for_unknown_user():
    respx.get("https://api.github.com/users/nobody/events").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    response = client.get("/stats/nobody")
    assert response.status_code == 404

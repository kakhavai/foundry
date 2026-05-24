from collections import Counter

import httpx

GITHUB_API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def get_events(username: str, client: httpx.AsyncClient) -> list[dict]:
    response = await client.get(
        f"{GITHUB_API}/users/{username}/events", headers=HEADERS
    )
    response.raise_for_status()
    events = response.json()
    return [
        {
            "type": e["type"],
            "repo": e["repo"]["name"],
            "created_at": e["created_at"],
        }
        for e in events[:10]
    ]


async def get_stats_data(username: str, client: httpx.AsyncClient) -> dict:
    response = await client.get(
        f"{GITHUB_API}/users/{username}/events", headers=HEADERS
    )
    response.raise_for_status()
    events = response.json()

    push_events = [e for e in events if e["type"] == "PushEvent"]
    pr_events = [e for e in events if e["type"] == "PullRequestEvent"]
    repo_counts = Counter(e["repo"]["name"] for e in events)

    return {
        "username": username,
        "commit_count": sum(e["payload"].get("distinct_size", 0) for e in push_events),
        "pr_count": len(pr_events),
        "top_repos": [repo for repo, _ in repo_counts.most_common(5)],
    }

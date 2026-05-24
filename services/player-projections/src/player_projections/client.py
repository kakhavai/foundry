import httpx


async def fetch_projections(url: str, api_key: str) -> list[dict]:
    """Fetch player projection data from the upstream player-data service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return response.json().get("players", [])

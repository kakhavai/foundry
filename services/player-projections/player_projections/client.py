import httpx


async def fetch_projections(url: str) -> list[dict]:
    """Fetch player projections from the S3 file written by the player-data backend."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json().get("players", [])

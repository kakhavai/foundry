import json

import httpx


class MalformedSnapshotError(ValueError):
    """The upstream snapshot was not a JSON object with the expected shape."""


async def fetch_projections(url: str) -> list[dict]:
    """Fetch player projections from the S3 file written by the player-data backend.

    Raises:
        httpx.HTTPStatusError: the upstream returned a 4xx or 5xx.
        MalformedSnapshotError: the body was not valid JSON, or not a JSON object.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise MalformedSnapshotError(
                f"expected valid JSON at {url}: {exc}"
            ) from exc

    if not isinstance(body, dict):
        raise MalformedSnapshotError(
            f"expected a JSON object at {url}, got {type(body).__name__}"
        )

    players = body.get("players", [])
    if not isinstance(players, list):
        raise MalformedSnapshotError(
            f"expected `players` to be a list, got {type(players).__name__}"
        )
    return players

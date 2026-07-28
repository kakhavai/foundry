import json

import httpx


class MalformedSnapshotError(ValueError):
    """The upstream snapshot was not a JSON object with the expected shape."""


async def fetch_projections(url: str, expect_format: str | None = None) -> list[dict]:
    """Fetch player projections from the S3 file written by the player-data backend.

    `expect_format` is the scoring format this URL is supposed to serve. The
    schema's `format` is an enum across all three modes, so it cannot pin any
    one document to its own URL — that check has to happen here, against what
    the caller was configured to expect. It catches the misconfiguration that
    actually matters in production: the PPR document published, or polled, at
    the standard URL. Passing None skips the check.

    Raises:
        httpx.HTTPStatusError: the upstream returned a 4xx or 5xx.
        MalformedSnapshotError: the body was not valid JSON, not a JSON object,
            was not decodable text (invalid encoding), or declared a scoring
            format other than `expect_format`.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MalformedSnapshotError(
                f"expected valid JSON at {url}: {exc}"
            ) from exc

    if not isinstance(body, dict):
        raise MalformedSnapshotError(
            f"expected a JSON object at {url}, got {type(body).__name__}"
        )

    if expect_format is not None and body.get("format") != expect_format:
        raise MalformedSnapshotError(
            f"expected a {expect_format!r} snapshot at {url}, "
            f"got {body.get('format')!r}"
        )

    players = body.get("players", [])
    if not isinstance(players, list):
        raise MalformedSnapshotError(
            f"expected `players` to be a list, got {type(players).__name__}"
        )
    return [p for p in players if isinstance(p, dict)]

"""Resolving an upstream's native ids to canonical `fdy-` player ids.

`player-identity` is authoritative. `resolved` is the answer; `candidates`
is the working. Anything not explicitly `resolved: true` is unresolved --
that service already filed the miss server-side, and a caller that re-ranks
candidates against its own floor adopts an identity it explicitly refused.
"""

from dataclasses import asdict, dataclass

BATCH_LIMIT = 500


class UnresolvedPlayer(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolveQuery:
    name: str | None = None
    team: str | None = None
    position: str | None = None
    source: str | None = None
    source_id: str | None = None


class IdentityClient:
    def __init__(self, base_url: str, client, token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def resolve_many(
        self, queries: list[ResolveQuery]
    ) -> dict[ResolveQuery, str]:
        """Map only the queries player-identity RESOLVED. Unresolved ones are
        absent from the result -- the caller counts them in coverage.missing."""
        resolved: dict[ResolveQuery, str] = {}
        for start in range(0, len(queries), BATCH_LIMIT):
            chunk = queries[start : start + BATCH_LIMIT]
            response = await self._client.post(
                f"{self._base_url}/resolve/batch",
                json={"queries": [asdict(q) for q in chunk]},
                headers=self._headers(),
            )
            response.raise_for_status()
            for query, result in zip(chunk, response.json()["results"]):
                # `is True`, not truthiness: a missing field must not be
                # permission, and neither must a non-empty candidate list.
                if result.get("resolved") is True and result.get("player_id"):
                    resolved[query] = result["player_id"]
        return resolved

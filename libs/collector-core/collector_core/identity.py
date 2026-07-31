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
    def __init__(
        self,
        base_url: str,
        client,
        token: str | None = None,
        crosswalk_version: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._token = token
        self._crosswalk_version = crosswalk_version
        # Per-instance, in-memory only -- no TTL, no disk, no eviction.
        # Keyed on (crosswalk_version, query) so a republished crosswalk
        # invalidates cleanly instead of serving stale ids for a season.
        self._cache: dict[tuple[str | None, ResolveQuery], str] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def resolve_many(
        self, queries: list[ResolveQuery]
    ) -> dict[ResolveQuery, str]:
        """Map only the queries player-identity RESOLVED. Unresolved ones are
        absent from the result -- the caller counts them in coverage.missing.

        Successful resolutions are cached per instance, keyed on the
        crosswalk version supplied at construction. Unresolved queries are
        never cached -- a miss may become a hit once the crosswalk is
        republished mid-season, and caching the refusal would pin that gap
        until the process restarts.
        """
        resolved: dict[ResolveQuery, str] = {}
        pending: list[ResolveQuery] = []
        for query in queries:
            cached = self._cache.get((self._crosswalk_version, query))
            if cached is not None:
                resolved[query] = cached
            else:
                pending.append(query)

        for start in range(0, len(pending), BATCH_LIMIT):
            chunk = pending[start : start + BATCH_LIMIT]
            response = await self._client.post(
                f"{self._base_url}/resolve/batch",
                json={"queries": [asdict(q) for q in chunk]},
                headers=self._headers(),
            )
            response.raise_for_status()
            for query, result in zip(chunk, response.json()["results"], strict=True):
                # `is True`, not truthiness: a missing field must not be
                # permission, and neither must a non-empty candidate list.
                if result.get("resolved") is True and result.get("player_id"):
                    player_id = result["player_id"]
                    resolved[query] = player_id
                    self._cache[(self._crosswalk_version, query)] = player_id
        return resolved

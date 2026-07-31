"""Resolving an upstream's native ids to canonical `fdy-` player ids.

`player-identity` is authoritative. `resolved` is the answer; `candidates`
is the working. Anything not explicitly `resolved: true` is unresolved --
that service already filed the miss server-side, and a caller that re-ranks
candidates against its own floor adopts an identity it explicitly refused.
"""

from dataclasses import asdict, dataclass

import httpx

# Pinned to `player-identity`'s own `MAX_BATCH_QUERIES`
# (services/player-identity/player_identity/resolution.py) -- there is no
# import across the service/lib boundary to enforce agreement mechanically
# (collector-core cannot depend on a service), so
# `tests/test_identity_batch_limit.py` at the repo root reads both constants
# by AST and fails the build if they drift. A lowered server cap would
# otherwise show up as a silent 422 on every collector's next batch at once.
BATCH_LIMIT = 500


class UnresolvedPlayer(Exception):
    """Carries `.reason`, matching every other refusal shape in this repo
    (`roster_scope.adapters.identity.UnresolvablePlayer`,
    `player_stats.adapters.identity.UnresolvedPlayer`).

    `IdentityClient` itself never raises this -- see `resolve_many`'s
    docstring for why a batch failure must surface as data
    (`IdentityClient.failures`) rather than an exception. It is exported for
    a caller that wants to escalate one of those failures into a hard error
    on its own terms.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolveQuery:
    name: str | None = None
    team: str | None = None
    position: str | None = None
    # Both accepted and scored by `player-identity` (`api.build_query` reads
    # them straight off the request body) and both load-bearing for
    # resolution quality: `jersey_number` carries 0.20 of `WEIGHTS` in
    # `player_identity.resolution`, on par with `team`, and `season` lets a
    # backfill compare against the right week's truth via `ResolveQuery.
    # as_of` staleness discounting server-side. `roster-scope`'s own
    # `HttpPlayerIdentityResolver` already sends `jersey_number` today --
    # omitting either here would make a collector migrating onto this shared
    # seam resolve *worse* than the single-query code path it replaces.
    jersey_number: int | None = None
    season: int | None = None
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
        # The most recent `resolve_many` call's per-chunk request failures --
        # `query -> reason`. See `resolve_many`'s docstring for why this
        # exists as an attribute rather than a second return value or a
        # raised exception.
        self.failures: dict[ResolveQuery, str] = {}

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

        **A chunk player-identity could not be reached for is not a capture
        failure.** The design spec is explicit: "player-identity unreachable
        -> affected players resolve to nothing; each lands in
        coverage.missing with reason identity_unresolved. The pass still
        writes an envelope." A bare `raise_for_status()` used to propagate
        `httpx.ConnectError`/`HTTPStatusError` straight out of this method on
        a single 503 -- discarding every chunk that had already resolved and
        turning a partial outage into a hard capture failure. So a request
        that fails outright (a connection error, a timeout, a non-2xx
        status) is caught per chunk, and the loop moves on rather than
        aborting the rest of `queries`.

        **Recorded on `self.failures`, not folded into the returned dict.**
        Two shapes were considered and rejected:

        - Silently treating a failed chunk's queries the same as ones
          player-identity genuinely returned `resolved: false` for. Rejected
          because it is indistinguishable from an ordinary miss --
          `roster-scope`'s own `HttpPlayerIdentityResolver` keeps "identity
          said no" (`identity_unresolved_*`) and "the request to identity
          itself failed" (`identity_upstream_error`) as separate reasons for
          exactly this purpose, and a caller here should be able to do the
          same. A `player-identity` outage and a batch of genuine misses are
          different operational facts and must stay distinguishable in
          whatever `coverage.missing`/`errors` a caller builds from this.
        - A second return value (`tuple[dict, dict]`). Rejected because it
          is a breaking shape for the overwhelmingly common case -- every
          existing call site, in every collector this seam is used from,
          would have to unpack a tuple to reach a dict that is empty almost
          always.

        `self.failures` is reset at the top of every call, so it always
        describes the call that just returned, never a prior one.

        A response that comes back with the *wrong shape* (too few results,
        an unparseable body) is a `player-identity` contract violation, not
        a reachability problem, and is NOT caught here -- it continues to
        raise, per `test_a_short_response_raises_rather_than_silently_
        dropping_the_tail`.
        """
        self.failures = {}
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
            try:
                response = await self._client.post(
                    f"{self._base_url}/resolve/batch",
                    json={"queries": [asdict(q) for q in chunk]},
                    headers=self._headers(),
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                reason = f"identity_upstream_error: {exc}"
                for query in chunk:
                    self.failures[query] = reason
                continue
            for query, result in zip(chunk, response.json()["results"], strict=True):
                # `is True`, not truthiness: a missing field must not be
                # permission, and neither must a non-empty candidate list.
                if result.get("resolved") is True and result.get("player_id"):
                    player_id = result["player_id"]
                    resolved[query] = player_id
                    self._cache[(self._crosswalk_version, query)] = player_id
        return resolved

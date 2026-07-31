"""The `player-identity` seam — an upstream *player id* to a canonical one.

Every emitted row carries `player_id`, and the spec is unambiguous that it is
"canonical id from `player-identity`", not the feed's own key. That makes
identity a genuine upstream of this collector, so it gets an adapter like any
other.

This is a *crosswalk* lookup rather than `roster-scope`'s name resolution: the
box-score feed supplies a stable GSIS id per row, and `player-identity`
publishes exactly that in its `external_ids` block, so the query is
`GET /resolve?source=gsis&source_id=<id>` — a Tier-1 adopted link, not an
attribute score. Name matching is the fallback path this collector deliberately
does not take: a feed that already carries a league id and is matched by name
anyway is how two Josh Allens become one player.

`build_id_resolver` picks between two implementations on
`PLAYER_IDENTITY_URL`, exactly the way `roster-scope` picks its resolver and
`weather` picks its `SCHEDULE_URL`:

- `StubIdCrosswalk` is the default, because `PLAYER_IDENTITY_URL` ships empty.
  It mints a deterministic `fdy-` id from the GSIS id so a capture is stable
  across passes — a resolver that minted a new universe each pass would make
  every row look newly restated.
- `HttpIdCrosswalk` calls the real collector.

**The stub's ids do not join with `roster-scope`'s stub ids.** The two stubs
hash different inputs (a GSIS id here, `name|team|position` there), which used
to be why the watchlist in `adapters/scope.py` was unreliable while both
services ran unconfigured. That module now reads the watchlist from the lake
unconditionally, with no env var of its own — leaving `PLAYER_IDENTITY_URL`
empty here still means the join runs stub-vs-real, which shows up loudly as
`coverage.missing` naming every watchlist player rather than quietly as a
short list of rows.
"""

import hashlib
import os
from typing import Protocol, runtime_checkable

import httpx

# Empty means "no player-identity deployment to talk to". Read at call time,
# not import time, so a test or a redeploy can change it without reimporting.
PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"

# The `external_ids` source name this feed's `player_id` column belongs to.
# nflverse keys weekly stats by GSIS id (`00-0034796`).
CROSSWALK_SOURCE = "gsis"

# Below this a candidate is a tie or a coin-flip. A crosswalk hit should score
# at or near 1.0, so anything under this means the lookup fell back to
# attribute scoring and must not be adopted as if it were a published link.
MIN_CONFIDENCE = 0.9


class UnresolvedPlayer(Exception):
    """The crosswalk declined to name this player.

    Carries a `reason` because the caller records it verbatim against the
    coverage key — "the id is not in the crosswalk" and "player-identity is
    down" must not collapse into one undifferentiated hole.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@runtime_checkable
class IdCrosswalk(Protocol):
    async def resolve(self, upstream_player_id: str) -> str:
        """Canonical `player_id`, or raise `UnresolvedPlayer`."""
        ...


class StubIdCrosswalk:
    """Deterministic stand-in while `PLAYER_IDENTITY_URL` is empty.

    `fdy-<sha256("gsis|<id>")[:12]>`. Deterministic so the same feed row
    resolves to the same id on every pass; without that, `revisions.py` would
    see a brand-new key every capture and report the whole week as restated.

    It refuses rather than guesses on a blank id: a stable id derived from
    nothing looks resolved to every downstream consumer, which is worse than
    an absent one.
    """

    async def resolve(self, upstream_player_id: str) -> str:
        key = (upstream_player_id or "").strip()
        if not key:
            raise UnresolvedPlayer("identity_missing_source_id")
        digest = hashlib.sha256(f"{CROSSWALK_SOURCE}|{key}".encode()).hexdigest()
        return f"fdy-{digest[:12]}"


class HttpIdCrosswalk:
    """Calls the real `player-identity` collector, one row at a time.

    The shape is read off `services/player-identity/player_identity/api.py`'s
    `resolve_query`, not guessed: `GET /resolve` accepts `source`/`source_id`
    and answers `{"resolved": bool, "player_id": str|null, "confidence":
    float, ...}`.

    A per-row `GET` rather than `POST /resolve/batch` (capped at 500) because a
    week is under a thousand rows and the responses are cached below, so the
    second pass over a season costs nothing. Batching is the obvious upgrade if
    that ever stops being true.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._cache: dict[str, str] = {}

    async def resolve(self, upstream_player_id: str) -> str:
        key = (upstream_player_id or "").strip()
        if not key:
            raise UnresolvedPlayer("identity_missing_source_id")
        if key in self._cache:
            return self._cache[key]

        headers = {}
        token = os.getenv("COLLECTOR_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._client.get(
                f"{self._base_url}/resolve",
                params={"source": CROSSWALK_SOURCE, "source_id": key},
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 — every failure is one missing row
            raise UnresolvedPlayer("identity_upstream_error", str(exc)) from exc

        player_id = body.get("player_id")
        if not body.get("resolved") or not player_id:
            raise UnresolvedPlayer("identity_not_in_crosswalk", key)
        if float(body.get("confidence") or 0.0) < MIN_CONFIDENCE:
            raise UnresolvedPlayer("identity_low_confidence", key)

        self._cache[key] = str(player_id)
        return self._cache[key]


def build_id_resolver(client: httpx.AsyncClient) -> IdCrosswalk:
    """HTTP iff `PLAYER_IDENTITY_URL` is set; the deterministic stub otherwise."""
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if base_url:
        return HttpIdCrosswalk(client, base_url)
    return StubIdCrosswalk()

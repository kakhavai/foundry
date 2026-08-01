"""Resolving each opportunity's player through `player-identity`.

This collector publishes no `player_id` -- its rows are keyed by (defense,
position, alignment, scoring_format). Identity is a **gate**, not an output:
the spec requires every allowed opportunity to resolve to a canonical
`player_id` before it is attributed to a position, so an opportunity whose
player `player-identity` declined to name contributes to nothing.

Three rules from `docs/collectors.md`, all of them load-bearing:

**`resolve_many` chunks internally at `BATCH_LIMIT` (500).** There is no
caller-side batching here, deliberately -- a second chunker would be a second
place for the limit to drift from `player-identity`'s own `MAX_BATCH_QUERIES`,
which the repo-root `tests/test_identity_batch_limit.py` pins.

**`resolved.get(query)`, never `resolved.get(query, raw_id)`.** The default is
the whole non-negotiable. `player-identity` is authoritative; `candidates` is
its working and `resolved` is its answer. A defaulting `.get` is positionally
safe -- the code runs, the types line up, every test stays green -- and it
adopts the upstream's own GSIS key for exactly the players the authoritative
service refused to name.

**A failed request is not a refusal.** `resolve_many` catches per chunk and
records `query -> reason` on `IdentityClient.failures`, so a `player-identity`
outage is *absent from the returned dict* in precisely the same way a genuine
`resolved: false` is. Read only the dict and a total outage reports itself as
an ordinary quiet week whose `errors` array says nothing but
`below_expected_floor`. `IdentityFailures` is the summarised roll-up, one
entry per pass -- `usage-share`'s is the model, and 650 per-row entries would
fill `CoverageAccumulator`'s 50-entry cap by themselves.

--------------------------------------------------------------------------
`position` is sanitised before it is sent, and that is not defensive
--------------------------------------------------------------------------

`player-identity`'s `build_query` raises `HTTPException(422)` for a position
outside `KNOWN_POSITIONS`, and `resolve_queries` calls it inside the loop over
the batch -- so **one unmapped position code fails the whole 500-query
request**, not one row of it. `IdentityClient` then records the 422 as an
`identity_upstream_error` for all 500 and moves on, and the pass silently
loses every one of them.

That is live, and it reproduces against this collector's own upstream:
nflverse `players.csv` publishes 25 distinct position codes, of which exactly
one -- `SAF`, 345 players -- is absent from `KNOWN_POSITIONS` (which carries
`S`, `FS` and `SS` but not `SAF`). Three players with 2025 opportunities carry
it.

This collector does not hit it today, because `ROSTER_TO_FANTASY` narrows to
six codes before anything is resolved and all six are in `KNOWN_POSITIONS`.
That is a *consequence* of another decision, though, so it would survive
exactly until somebody added a code to that map. `SENDABLE_POSITIONS` makes it
an assertion instead: an unrecognised code travels as `None` -- costing one
scoring signal for one query -- rather than as a 422 costing 500 resolutions.
"""

import os
from dataclasses import dataclass, field

import httpx
from collector_core.identity import IdentityClient, ResolveQuery

from .players import PlayerRef

# The crosswalk source play-by-play is keyed by. Tier 1 in `player-identity`'s
# ladder: adopted exactly, never scored.
UPSTREAM_SOURCE = "gsis"

PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"

# The `errors` reason for "this pod was never pointed at player-identity" --
# a configuration fact, distinct from a request that failed and distinct again
# from a genuine refusal.
IDENTITY_UNAVAILABLE = "identity_unavailable"
IDENTITY_UPSTREAM_ERROR = "identity_upstream_error"
IDENTITY_UNRESOLVED = "identity_unresolved"

# Positions this collector will send. A mirror of the subset of
# `player-identity`'s `KNOWN_POSITIONS` that `ROSTER_TO_FANTASY` can produce,
# written out rather than imported because collector-core cannot depend on a
# service and neither can this. See the module docstring for what an unlisted
# code costs if it is sent anyway.
SENDABLE_POSITIONS = frozenset({"QB", "RB", "FB", "WR", "TE", "K"})

MAX_FAILURE_DETAIL_CHARS = 200


class IdentityUnavailable(Exception):
    """No `player-identity` to talk to. Carries `.reason`."""

    def __init__(self, reason: str = IDENTITY_UNAVAILABLE) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class IdentityFailures:
    """Players lost to a failed `player-identity` REQUEST, not a refusal.

    Accumulated across every chunk of one pass, because
    `IdentityClient.failures` is reset at the top of its next call and so only
    ever describes the last one.
    """

    rows: int = 0
    reasons: list[str] = field(default_factory=list)

    def record(self, failures: dict) -> None:
        self.rows += len(failures)
        for reason in failures.values():
            if reason not in self.reasons:
                self.reasons.append(reason)

    def detail(self) -> str:
        reasons = "; ".join(self.reasons)[:MAX_FAILURE_DETAIL_CHARS]
        return f"{self.rows} player(s) unresolved: {reasons}"


def build_identity_client(client: httpx.AsyncClient) -> IdentityClient:
    """The `player-identity` seam, or `IdentityUnavailable`.

    Raises rather than returning a stub resolver. Every stub in this fleet
    mints an `fdy-` id from a hash of the upstream key; here that would gate
    the attribution on an identity `player-identity` never issued, which is a
    total join failure that publishes a complete-looking set of ratings.
    """
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if not base_url:
        raise IdentityUnavailable()
    token = os.getenv("COLLECTOR_TOKEN", "").strip()
    return IdentityClient(base_url, client, token=token or None)


def build_query(reference: PlayerRef, season: int) -> ResolveQuery:
    """One player as a resolve query.

    `source`/`source_id` are what earn the Tier-1 adoption. `team`, `position`,
    `jersey_number` and `season` travel with them so `player-identity` can
    reject a crosswalk hit that contradicts the roster row it came from --
    every one of them is scored server-side, and `jersey_number` alone carries
    0.20 of the weighting.

    No `name`. A GSIS id absent from the crosswalk would otherwise fall
    through to attribute scoring, and "a feed that already carries a league id
    and is matched by name anyway is how two Josh Allens become one player".
    """
    position = reference.roster_position
    return ResolveQuery(
        team=reference.team,
        position=position if position in SENDABLE_POSITIONS else None,
        jersey_number=reference.jersey_number,
        season=season,
        source=UPSTREAM_SOURCE,
        source_id=reference.gsis_id,
    )


async def resolve_players(
    player_ids: set[str],
    *,
    season: int,
    positions: dict[str, PlayerRef],
    identity: IdentityClient,
    failures: IdentityFailures,
) -> set[str]:
    """The GSIS ids `player-identity` resolved. Everything else is dropped.

    Returns GSIS ids rather than the canonical `fdy-` ids because the fold is
    keyed by the upstream id and the canonical id appears in no published
    field -- what matters here is *which* opportunities passed the gate, not
    what they are called. The canonical ids are still what was asked for and
    what was checked; discarding them after the check is not the same as never
    asking, and `test_a_raw_upstream_id_is_never_adopted_for_a_refused_player`
    pins the difference.

    No caller-side chunking: `resolve_many` does it at `BATCH_LIMIT`.
    """
    queries = [
        (gsis_id, build_query(positions[gsis_id], season))
        for gsis_id in sorted(player_ids)
        if gsis_id in positions
    ]
    if not queries:
        return set()

    resolved = await identity.resolve_many([query for _, query in queries])
    # Read immediately: the attribute describes the call that just returned.
    failures.record(identity.failures)

    kept: set[str] = set()
    for gsis_id, query in queries:
        # `.get(query)`. No default -- see the module docstring.
        canonical = resolved.get(query)
        if canonical is not None:
            kept.add(gsis_id)
    return kept

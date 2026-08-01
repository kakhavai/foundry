"""Narrowing this collector to `roster-scope`'s membership list.

**Forward, not backward.** The scope is a set of canonical `fdy-` ids and every
feed here is keyed by a league id (`gsis_id`, or `pfr_id` one hop further on),
so the join could run in either direction. It runs forward — every upstream
row's `gsis_id` is resolved through `player-identity` and the resulting `fdy-`
id is checked against the scope — because `gsis` is a *published crosswalk
source*, which `player-identity` adopts at Tier 1 with no attribute scoring at
all. `usage_share/adapters/scope.py` established the shape.

**The scope comes from the lake, never from `roster-scope` over HTTP.** The lake
is append-only and already carries every scope capture, so the last good scope
survives a `roster-scope` outage.

**Fail closed, and that includes the identity seam.** No scope means zero
upstream calls and a `present: 0` envelope. There is deliberately no unnarrowed
fallback: the five feeds are 43.8 MB on a cold window, and spending that to
publish nothing is exactly the waste failing closed exists to prevent. An empty
`PLAYER_IDENTITY_URL` is the same fact wearing a different hat — without it not
one row can be resolved, so not one row could ever be in scope. Both raise
`ScopeUnavailable`, differing only in `.reason`, so `capture.py` has one refusal
path rather than two.

**No name is sent, deliberately.** `ResolveQuery` accepts one and this adapter
leaves it `None`. A GSIS id absent from the crosswalk would otherwise fall
through to attribute scoring, and a feed that already carries a league id and is
matched by name anyway is how two Josh Allens become one player.

**Team defenses are not players.** `roster-scope` mints them as
`fdy-dst-<team>`, one per club, and a team defense has no hamstring to strain.
They are recognised by that prefix rather than by re-reading the scope
envelope's `entity_type` column — `ScopeClient` returns ids only, and
re-implementing it to reach one field would fork the fleet's one narrowing seam.
This is NOT expectation derived from success: the prefix is a fact about how
`roster-scope` constructs the id, decided before any fetch.
"""

import os
from dataclasses import dataclass, field

import httpx
from collector_core.identity import IdentityClient, ResolveQuery
from collector_core.scope import Scope, ScopeClient, ScopeUnavailable

from .upstream import PlayerRow

__all__ = [
    "IDENTITY_UNAVAILABLE",
    "IDENTITY_UPSTREAM_ERROR",
    "SCOPE_SIGNAL_TYPE",
    "TEAM_DEFENSE_PREFIX",
    "IdentityFailures",
    "build_identity_client",
    "fetch_scope",
    "individual_players",
    "resolve_in_scope",
]

# The scope list this collector narrows to. Membership only: a durability record
# is one player's own history, so the matchup list (`injury-report`'s other
# half) names nobody this collector owes a record to.
SCOPE_SIGNAL_TYPE = "scope_membership_weekly"

# The crosswalk source every feed here is keyed by. Tier 1 in `player-identity`'s
# resolution ladder: adopted exactly, never scored.
UPSTREAM_SOURCE = "gsis"

# See the module docstring. `roster_scope.scope` builds these as
# `f"fdy-dst-{team.lower()}"`.
TEAM_DEFENSE_PREFIX = "fdy-dst-"

PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"

# `ScopeUnavailable.reason` for the identity half of the refusal, kept distinct
# from `scope_unavailable`/`scope_empty` so an operator reading the envelope can
# tell "roster-scope published nothing" from "this pod was never pointed at
# player-identity". They are different fixes.
IDENTITY_UNAVAILABLE = "identity_unavailable"

# The `errors` reason for the OTHER identity failure: `player-identity` was
# configured and reached for, and the request itself failed. Distinct from
# `IDENTITY_UNAVAILABLE` (the config case) and from a genuine `resolved: false`
# refusal, which is `player-identity` doing its job.
IDENTITY_UPSTREAM_ERROR = "identity_upstream_error"

MAX_FAILURE_DETAIL_CHARS = 200


@dataclass
class IdentityFailures:
    """Rows this pass lost to a failed `player-identity` REQUEST, not a refusal.

    `IdentityClient.resolve_many` returns a **partial** dict when a chunk's
    request fails: the affected queries are simply absent from the result and
    the reason is recorded on `IdentityClient.failures` instead. A caller that
    reads only the dict therefore treats a `player-identity` outage exactly like
    an ordinary miss, and publishes a short envelope whose `errors` array says
    nothing but `below_expected_floor` — indistinguishable from a truncated
    scope or a truncated feed.

    **Summarised, never per row.** A total outage against a ~1,400-row feed would
    otherwise file 1,400 entries and push every other error past
    `CoverageAccumulator`'s 50-entry cap.
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
        return f"{self.rows} row(s) unresolved: {reasons}"


async def fetch_scope(lake, season: int, week: int) -> Scope:
    """The membership list for `(season, week)`, or `ScopeUnavailable`.

    A thin pass-through to `ScopeClient` — including its fall back to `week - 1`,
    which is what stops every week rollover failing this collector closed until
    `roster-scope`'s weekly capture lands.
    """
    return await ScopeClient(lake).fetch(SCOPE_SIGNAL_TYPE, season, week)


def individual_players(scope: Scope) -> frozenset[str]:
    """The scope's individual players — every member that is not a team defense.

    This is the universe a durability record is OWED for, and it is what
    `capture.py` calls `acc.expect` over.
    """
    return frozenset(
        member for member in scope.members if not member.startswith(TEAM_DEFENSE_PREFIX)
    )


def build_identity_client(client: httpx.AsyncClient) -> IdentityClient:
    """The `player-identity` seam, or `ScopeUnavailable` if there is none.

    Raises rather than returning a stub. Every stub in this fleet mints an
    `fdy-` id from a hash of the upstream key, and here that would be an
    identity `player-identity` never issued being checked against a scope
    `player-identity` did issue — a total join failure dressed up as a working
    one, which would publish an empty durability table while reporting a healthy
    fetch.
    """
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if not base_url:
        raise ScopeUnavailable(IDENTITY_UNAVAILABLE)
    token = os.getenv("COLLECTOR_TOKEN", "").strip()
    return IdentityClient(base_url, client, token=token or None)


def _query(row: PlayerRow, season: int) -> ResolveQuery:
    """One upstream row as a resolve query.

    `source`/`source_id` alone are what earn the Tier-1 adoption; `team`,
    `position`, `jersey_number` and `season` travel with them so
    `player-identity` can reject a crosswalk hit that contradicts the row it
    came from. No `name` — see the module docstring.
    """
    return ResolveQuery(
        team=row.team,
        position=row.position,
        jersey_number=row.jersey_number,
        season=season,
        source=UPSTREAM_SOURCE,
        source_id=row.gsis_id,
    )


async def resolve_in_scope(
    rows,
    *,
    season: int,
    scope_members: frozenset[str],
    identity: IdentityClient,
    failures: IdentityFailures,
) -> list[tuple[PlayerRow, str]]:
    """`(row, player_id)` for every row resolving into `scope_members`.

    Three outcomes, and only the first is published:

    * resolved AND in scope -> kept, under the canonical `fdy-` id
    * resolved but NOT in scope -> dropped; this is the narrowing
    * unresolved -> dropped. `player-identity` already filed the miss
      server-side, and adopting a candidate it refused would attribute one
      player's injury history to another — which for this collector means
      inventing a durability problem for somebody who has never been hurt.

    A dropped row is deliberately NOT recorded in `coverage.missing` by this
    function: coverage here is keyed by SCOPE SLOT rather than by upstream row,
    and `capture.py` expects every individual scope member up front and fails the
    ones no kept row covers. Counting dropped upstream rows instead would make
    every out-of-scope player read as a permanent coverage regression.

    **The queries are handed over in one call, and the chunking is
    `IdentityClient.resolve_many`'s.** It already splits at `BATCH_LIMIT` (500)
    before it posts, so a caller-side buffer changes nothing about what goes on
    the wire — `player-profile` had to delete exactly such a buffer after
    mutation testing showed disabling it left the HTTP traffic byte-identical.
    """
    queries = [(row, _query(row, season)) for row in rows]
    if not queries:
        return []
    resolved = await identity.resolve_many([query for _, query in queries])
    # Read immediately: `resolve_many` resets `failures` at the top of its next
    # call, so the attribute only ever describes the batch that just returned.
    failures.record(identity.failures)

    kept: list[tuple[PlayerRow, str]] = []
    for row, query in queries:
        # `.get(query)`, never `.get(query, row.gsis_id)`. The default is the
        # whole non-negotiable: `player-identity` is authoritative, and a row it
        # did not resolve must be dropped rather than published under the feed's
        # own GSIS key. `candidates` is its working, `resolved` is its answer,
        # and this reads only the answer.
        player_id = resolved.get(query)
        if player_id is not None and player_id in scope_members:
            kept.append((row, player_id))
    return kept

"""Narrowing this collector to `roster-scope`'s membership list.

**Forward, not backward, and — uniquely in this fleet — by two different keys.**
The scope is a set of canonical `fdy-` ids; the upstream is keyed by a display
name (`"Aaron Rodgers"`), an OverTheCap id, and — on 2,251 of its 2,931 active
rows (76.8%) — a `gsis_id`. Every upstream row is resolved through
`player-identity` and the resulting `fdy-` id is checked against the scope.

**Two query shapes, one per arm, and the split is deliberate.** `gsis` is a
*published crosswalk source* in `player_identity.identity.CROSSWALK_KEYS`,
adopted at Tier 1 with no attribute scoring at all:

* **`gsis_id` present -> `source`/`source_id` and NO name.** This is
  `player-profile`'s rule and its reasoning applies unchanged: a GSIS id absent
  from the crosswalk would otherwise fall through to attribute scoring, and a
  feed that already carries a league id and is matched by name anyway is how two
  Josh Allens become one player. `team` and `position` still travel, so
  `player-identity` can reject a crosswalk hit that contradicts the row it came
  from.
* **`gsis_id` absent -> name, team and position.** Tier 3 weighted agreement,
  where `normalized_key` carries 0.30, `team` 0.20 and `position_group` 0.15.
  There is no other route for these 680 rows, and `MIN_AGREEING_ATTRIBUTES` is
  2, so the name alone would not resolve one anyway.

Before the parquet switch every row took the second path, which made this the
only scope-aware collector in the fleet joining without a crosswalk id. Three
consequences of that path are still load-bearing for the 23% that take it:

* **A blank name is dropped by the adapter** before it ever gets here — a row
  with neither a name nor a `gsis_id` is one `player-identity` could not resolve
  by any route.
* **A wrong `team` is worse than no team.** It scores as disagreement, not as
  absence. `canonical_team` returns `None` for the 64 ambiguous multi-club rows
  and for anything unrecognised, rather than guessing.
* **An unknown `position` fails the whole batch.** `player-identity`'s
  `build_query` raises 422 on a position outside `KNOWN_POSITIONS`, and
  `/resolve/batch` validates the whole body, so one bad code costs all 500
  queries in its chunk — including every Tier-1 query travelling with it.
  `canonical_position` maps or returns `None`.

**`otc_id` is NOT sent as `source_id`.** It is a real, stable OverTheCap
identifier, and it is not a crosswalk one: `CROSSWALK_KEYS` carries `gsis`,
`espn`, `yahoo`, `rotowire`, `sportradar` and `fantasy_data`, and the index is
built from Sleeper. `source="otc"` could therefore never match at Tier 1 (not a
crosswalk source) or Tier 2 (never recorded in any `external_ids`), so sending it
would be inert traffic dressed as a join. It is *published* on the signal row as
`otc_player_id` instead — see `capture.build_signal` for why that is worth doing
and why it can never be mistaken for a `player_id`.

**The scope comes from the lake, never from `roster-scope` over HTTP.** The
lake is append-only and already carries every scope capture, so the last good
scope survives a `roster-scope` outage.

**Fail closed, and that includes the identity seam.** No scope means zero
upstream calls and a `present: 0` envelope. There is deliberately no unnarrowed
fallback. An empty `PLAYER_IDENTITY_URL` is the same fact wearing a different
hat — without it not one row can be resolved, so not one row could ever be in
scope. Both raise `ScopeUnavailable`, differing only in `.reason`, so
`capture.py` has one refusal path rather than two.

**Team defenses are not players and cannot hold a contract.** `roster-scope`
mints them as `fdy-dst-<team>` (`roster_scope.scope`, the
`entity_type == "team_defense"` branch), one per club — thirteen of the 416
config slots being the twelve individual ones plus that. They are recognised by
that prefix rather than by re-reading the scope envelope's `entity_type`
column, because `ScopeClient` returns ids only and re-implementing it to reach
one field would fork the fleet's one narrowing seam. This is NOT expectation
derived from success: the prefix is a fact about how `roster-scope` constructs
the id, decided before any fetch.
"""

import os
from dataclasses import dataclass, field

import httpx
from collector_core.identity import IdentityClient, ResolveQuery
from collector_core.scope import Scope, ScopeClient, ScopeUnavailable

from .upstream import ContractRow, canonical_position, canonical_team

__all__ = [
    "IDENTITY_UNAVAILABLE",
    "IDENTITY_UPSTREAM_ERROR",
    "SCOPE_SIGNAL_TYPE",
    "TEAM_DEFENSE_PREFIX",
    "UPSTREAM_CROSSWALK_SOURCE",
    "IdentityFailures",
    "build_identity_client",
    "fetch_scope",
    "individual_players",
    "resolve_in_scope",
]

# The scope list this collector narrows to. Membership only: a contract is one
# player's own financial commitment, so the matchup list (`injury-report`'s
# other half) names nobody this collector owes a record to.
SCOPE_SIGNAL_TYPE = "scope_membership_weekly"

# The crosswalk source the parquet's `gsis_id` column belongs to. Tier 1 in
# `player-identity`'s resolution ladder: adopted exactly, never scored. Must
# stay one of `player_identity.identity.CROSSWALK_SOURCES` — a source name it
# does not know is not an error, it is silently inert, which is the whole reason
# `otc` is not sent.
UPSTREAM_CROSSWALK_SOURCE = "gsis"

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

    That failure is unusually easy to hit here. One unmapped position code 422s
    an entire 500-query chunk, so the difference between "nobody resolved" and
    "one column changed shape upstream" lives in this object.

    Accumulated across every batch of one pass, because `resolve_many` resets
    `failures` on each call.

    **Summarised, never per row.** A total outage against a 2,908-row feed would
    otherwise file 2,908 entries and push every other error past
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

    This is the universe a contract record is OWED for, and it is what
    `capture.py` calls `acc.expect` over. See the module docstring for why a
    prefix test is a fact about `roster-scope`'s construction rather than a
    derivation from what this pass managed to fetch.
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
    one, publishing an empty contract table while reporting a healthy fetch.
    """
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if not base_url:
        raise ScopeUnavailable(IDENTITY_UNAVAILABLE)
    token = os.getenv("COLLECTOR_TOKEN", "").strip()
    return IdentityClient(base_url, client, token=token or None)


def _query(row: ContractRow, season: int) -> ResolveQuery:
    """One upstream row as a resolve query — Tier 1 if it can be, else Tier 3.

    See the module docstring for the split. The short version: a `gsis_id` earns
    a crosswalk adoption and the name is deliberately withheld so a crosswalk
    miss cannot silently fall through to name scoring; without one, the name is
    the only route there is.

    `team` and `position` travel on both arms. On the Tier-1 arm they let
    `player-identity` reject a crosswalk hit that contradicts the row; on the
    Tier-3 arm they are what separates the QB Josh Allen from the edge rusher of
    the same name — twelve names are shared inside the active set alone. Both
    are canonicalised or omitted, never passed through raw.

    No `jersey_number`: the feed carries none.
    """
    team = canonical_team(row.otc_team)
    position = canonical_position(row.otc_position)
    if row.gsis_id:
        return ResolveQuery(
            team=team,
            position=position,
            season=season,
            source=UPSTREAM_CROSSWALK_SOURCE,
            source_id=row.gsis_id,
        )
    return ResolveQuery(
        name=row.player_name,
        team=team,
        position=position,
        season=season,
    )


async def resolve_in_scope(
    rows,
    *,
    season: int,
    scope_members: frozenset[str],
    identity: IdentityClient,
    failures: IdentityFailures,
) -> list[tuple[ContractRow, str]]:
    """`(row, player_id)` for every row resolving into `scope_members`.

    Three outcomes, and only the first is published:

    * resolved AND in scope -> kept, under the canonical `fdy-` id
    * resolved but NOT in scope -> dropped; this is the narrowing
    * unresolved -> dropped. `player-identity` already filed the miss
      server-side, and adopting a candidate it refused would attribute one
      player's contract, guarantee and contract year to another.

    A dropped row is deliberately NOT recorded in `coverage.missing` by this
    function: coverage here is keyed by SCOPE SLOT rather than by upstream row,
    and `capture.py` expects every individual scope member up front and fails
    the ones no kept row covers. That is the direction that makes a narrowing
    hole visible — counting dropped upstream rows instead would make every
    out-of-scope player read as a permanent coverage regression.

    **The queries are handed over in one call, and the chunking is
    `IdentityClient.resolve_many`'s.** It already splits at `BATCH_LIMIT` (500)
    before it posts, so a caller-side buffer changes nothing about what goes on
    the wire — `player-profile` proved that by mutation, where disabling its
    buffer left the HTTP traffic byte-identical. ~2,908 active rows is six
    chunks and a few hundred KB of query list.
    """
    queries = [(row, _query(row, season)) for row in rows]
    if not queries:
        return []
    resolved = await identity.resolve_many([query for _, query in queries])
    # Read immediately: `resolve_many` resets `failures` at the top of its next
    # call, so the attribute only ever describes the batch that just returned.
    failures.record(identity.failures)

    kept: list[tuple[ContractRow, str]] = []
    for row, query in queries:
        # `.get(query)`, never `.get(query, row.player_name)`, never
        # `.get(query, row.otc_id)`, never `.get(query, row.gsis_id)`.
        # The default is the whole non-negotiable:
        # `player-identity` is authoritative, and a row it did not resolve must
        # be dropped rather than published under the feed's own display name.
        # A defaulting `.get` is *positionally safe* — the adopted string then
        # fails the scope check and the row disappears anyway — so it survives
        # every end-to-end narrowing test while quietly making raw upstream
        # strings eligible to become canonical ids. `candidates` is
        # `player-identity`'s working, `resolved` is its answer, and this reads
        # only the answer.
        player_id = resolved.get(query)
        if player_id is not None and player_id in scope_members:
            kept.append((row, player_id))
    return kept

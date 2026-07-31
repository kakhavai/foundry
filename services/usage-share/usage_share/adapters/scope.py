"""Narrowing this collector to `roster-scope`'s membership list.

**Forward, not backward.** The scope is a set of canonical `fdy-` ids and the
feed is keyed by GSIS id, so the join could run in either direction. It runs
forward — every upstream row's `gsis_id` is resolved to an `fdy-` id and
checked against the scope — because `gsis` is a *published crosswalk source*,
which means `player-identity` adopts the link at Tier 1 with no scoring at all.
The reverse direction (turning the scope's `fdy-` ids into GSIS ids) has no
seam today and is deliberately left to 8C, where a per-player API genuinely
needs to name a player before fetching them.

This is why the registry's old claim that narrowing here was "impossible" was
wrong: it predates `collector_core.identity` and assumed the reverse direction.

**The scope comes from the lake, never from `roster-scope` over HTTP.** The
lake is append-only and already carries every scope capture, so the last good
scope survives a `roster-scope` outage — which is what stops one service being
a fleet-wide stop.

**Fail closed, and that includes the identity seam.** No scope means zero
upstream calls and a `present: 0` envelope; there is deliberately no unnarrowed
fallback, because one would blow the vendor's budget precisely during an
incident. `PLAYER_IDENTITY_URL` being empty is the same fact wearing a
different hat: without it not one row can be resolved, so not one row could
ever be in scope, and fetching an ~8.3 MB season CSV to publish nothing is
exactly the waste failing closed exists to prevent. Both raise
`ScopeUnavailable`, differing only in `.reason`, so `capture.py` has one
refusal path rather than two.

**No name is sent, deliberately.** `ResolveQuery` accepts one and this adapter
leaves it `None`. A GSIS id absent from the crosswalk would otherwise fall
through to attribute scoring, and `player_stats/adapters/identity.py` states
the reason not to want that: "a feed that already carries a league id and is
matched by name anyway is how two Josh Allens become one player". A miss stays
a miss, the row is dropped, and the shortfall shows up against
`EXPECTED_FLOOR`.

**Batched.** `IdentityClient.resolve_many` chunks at `BATCH_LIMIT` (500, pinned
to `player-identity`'s own `MAX_BATCH_QUERIES` by the repo-root
`tests/test_identity_batch_limit.py`). Rows are buffered to at most one batch
before being resolved and filtered, so no single query list is ever longer than
that. Note this bounds the *working set*, not the whole pass:
`IdentityClient._cache` retains every query it resolved for the life of the
client, which is one capture pass, so cache growth is O(feed) — a few hundred
KB at ~1,700 rows, nowhere near the 256Mi limit, but not "one batch" either.
"""

import os
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field

import httpx
from collector_core.identity import BATCH_LIMIT, IdentityClient, ResolveQuery
from collector_core.scope import Scope, ScopeClient, ScopeUnavailable

from .upstream import UsageRow

# The scope list this collector narrows to. Membership only: every signal it
# publishes is one offensive player's own opportunity, so the matchup list
# (`injury-report`'s other half) names nobody this feed has a row for.
SCOPE_SIGNAL_TYPE = "scope_membership_weekly"

# The crosswalk source the feed is keyed by. Tier 1 in `player-identity`'s
# resolution ladder: adopted exactly, never scored.
UPSTREAM_SOURCE = "gsis"

# Empty means "no player-identity deployment to talk to", which fails closed —
# see the module docstring. Read at call time, not import time, so a test or a
# redeploy can change it without reimporting.
PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"

# `ScopeUnavailable.reason` for the identity half of the refusal, kept distinct
# from `scope_unavailable`/`scope_empty` so an operator reading the envelope
# can tell "roster-scope published nothing" from "this pod was never pointed at
# player-identity". They are different fixes.
IDENTITY_UNAVAILABLE = "identity_unavailable"

# The `errors` reason for the OTHER identity failure: `player-identity` was
# configured and reached for, and the request itself failed — a 401, a
# connection refusal, a timeout. Distinct from `IDENTITY_UNAVAILABLE`, which is
# only ever the *config* case, and distinct again from a genuine refusal
# (`resolved: false`), which is `player-identity` doing its job. The name
# matches `roster_scope.adapters.identity` and `player_stats.adapters.identity`,
# both of which already separate "identity said no" from "the request to
# identity failed" under exactly this reason.
IDENTITY_UPSTREAM_ERROR = "identity_upstream_error"

# `IdentityClient.failures` carries the whole exception string per query, and a
# botocore- or httpx-style message can run to hundreds of characters. One
# summary entry lands in an append-only lake object, so it is bounded.
MAX_FAILURE_DETAIL_CHARS = 200


@dataclass
class IdentityFailures:
    """Rows this pass lost to a failed `player-identity` REQUEST, not a refusal.

    `IdentityClient.resolve_many` returns a **partial** dict when a chunk's
    request fails: the affected queries are simply absent from the result, and
    the reason is recorded on `IdentityClient.failures` instead — see that
    method's docstring for why it is data rather than an exception. A caller
    that reads only the dict therefore treats a `player-identity` outage
    exactly like an ordinary miss, and the pass publishes a short envelope
    whose `errors` array says nothing but `below_expected_floor` — which is
    indistinguishable from "the scope named two players" or "the feed was
    truncated". Three different incidents, one symptom.

    Accumulated across every batch of one pass because `resolve_many` resets
    `failures` on each call, so the attribute alone only ever describes the
    last chunk.

    **Summarised, never per row.** A total outage against a ~1,700-row feed
    would otherwise file ~1,700 entries and push every other error past
    `CoverageAccumulator`'s 50-entry cap — the same reasoning `player-stats`
    applies to its own `no_box_row` roll-up.
    """

    rows: int = 0
    reasons: list[str] = field(default_factory=list)

    def record(self, failures: dict) -> None:
        self.rows += len(failures)
        for reason in failures.values():
            if reason not in self.reasons:
                self.reasons.append(reason)

    def detail(self) -> str:
        """One line naming how many rows were lost and to what."""
        reasons = "; ".join(self.reasons)[:MAX_FAILURE_DETAIL_CHARS]
        return f"{self.rows} row(s) unresolved: {reasons}"


async def fetch_scope(lake, season: int, week: int) -> Scope:
    """The membership list for `(season, week)`, or `ScopeUnavailable`.

    A thin pass-through to `ScopeClient` — including its fall back to
    `week - 1`, which is what stops every week rollover failing this collector
    closed until `roster-scope`'s weekly capture lands.
    """
    return await ScopeClient(lake).fetch(SCOPE_SIGNAL_TYPE, season, week)


def build_identity_client(client: httpx.AsyncClient) -> IdentityClient:
    """The `player-identity` seam, or `ScopeUnavailable` if there is none.

    Raises rather than returning a stub. Every stub in this fleet mints an
    `fdy-` id from a hash of the upstream key, and here that would be an
    identity `player-identity` never issued being checked against a scope
    `player-identity` did issue — a total join failure dressed up as a working
    one.
    """
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if not base_url:
        raise ScopeUnavailable(IDENTITY_UNAVAILABLE)
    token = os.getenv("COLLECTOR_TOKEN", "").strip()
    return IdentityClient(base_url, client, token=token or None)


def _query(row: UsageRow, season: int) -> ResolveQuery:
    """One upstream row as a resolve query.

    `source`/`source_id` alone are what earn the Tier-1 adoption; `team`,
    `position` and `season` travel with them so `player-identity` can reject a
    crosswalk hit that contradicts the row it came from. No `name` — see the
    module docstring.
    """
    return ResolveQuery(
        team=row.team,
        position=row.position,
        season=season,
        source=UPSTREAM_SOURCE,
        source_id=row.upstream_player_id,
    )


async def _resolve_batch(
    batch: list[UsageRow],
    *,
    season: int,
    scope: Scope,
    identity: IdentityClient,
    failures: IdentityFailures,
) -> list[tuple[UsageRow, str]]:
    """One batch's kept `(row, player_id)` pairs.

    `resolve_many` returns ONLY the queries `player-identity` resolved, so a
    row missing from the result is unresolved — whether it was refused outright
    or the request for its chunk failed (`IdentityClient.failures`). Either way
    it is dropped here rather than published under a guessed id.

    The two are not the same *fact*, though, and only one of them is a
    `player-identity` incident, so the request failures are accumulated onto
    `failures` for the caller to report. Reading only the dict — as this used
    to — makes an outage silent: every row drops, and the envelope says nothing
    but `below_expected_floor`.
    """
    # Built once and carried alongside its row. `resolve_many` keys its result
    # on the query, so rebuilding one to look the answer up would make the join
    # depend on `_query` staying byte-identical across two call sites.
    queries = [(row, _query(row, season)) for row in batch]
    resolved = await identity.resolve_many([query for _, query in queries])
    # Read immediately: `resolve_many` resets `failures` at the top of its next
    # call, so the attribute only ever describes the batch that just returned.
    failures.record(identity.failures)
    kept: list[tuple[UsageRow, str]] = []
    for row, query in queries:
        # `.get(query)`, never `.get(query, row.upstream_player_id)`. The
        # default is the whole non-negotiable: `player-identity` is
        # authoritative, and a row it did not resolve must be dropped rather
        # than published under the feed's own GSIS key. The `in scope.members`
        # clause below happens to filter a raw GSIS id too, because it cannot
        # collide with a canonical `fdy-` member — that is a second, positional
        # accident, not the guard. `test_a_raw_upstream_id_is_never_adopted_
        # for_a_refused_row` isolates this one.
        player_id = resolved.get(query)
        if player_id is not None and player_id in scope.members:
            kept.append((row, player_id))
    return kept


async def resolve_in_scope(
    rows: Iterable[UsageRow],
    *,
    season: int,
    scope: Scope,
    identity: IdentityClient,
    failures: IdentityFailures,
) -> AsyncIterator[tuple[UsageRow, str]]:
    """Yield `(row, player_id)` for rows resolving into `scope.members`.

    Three outcomes, and only the first is published:

    * resolved AND in scope -> yielded, under the canonical `fdy-` id
    * resolved but NOT in scope -> dropped silently; this is the narrowing
    * unresolved -> dropped. `player-identity` already filed the miss
      server-side, and adopting a candidate it refused would attribute a real
      player's usage to the wrong id. `candidates` is the working, `resolved`
      is the answer, and this reads only the answer.

    A dropped row is deliberately NOT recorded in `coverage.missing`: it was
    never owed. An out-of-scope player is not a hole, and an unresolved one
    cannot be attributed to a scope slot without the very join that just
    failed. `EXPECTED_FLOOR` is what keeps a week that resolved almost nothing
    from reading as complete.

    `failures` is the one exception, and it is an out-parameter rather than a
    return value because this is an async generator: the rows have to stream,
    so there is nowhere to put a second value. The caller reads it once the
    iteration is done and files a single summarised `errors` entry — see
    `IdentityFailures`.
    """
    batch: list[UsageRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH_LIMIT:
            for pair in await _resolve_batch(
                batch,
                season=season,
                scope=scope,
                identity=identity,
                failures=failures,
            ):
                yield pair
            batch = []
    if batch:
        for pair in await _resolve_batch(
            batch, season=season, scope=scope, identity=identity, failures=failures
        ):
            yield pair

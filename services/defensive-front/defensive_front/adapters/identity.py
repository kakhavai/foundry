"""Resolving absent front starters to canonical `fdy-` ids.

Identity is used for **one field**: `key_absences`. Every other published
column is keyed by team, so nothing else here needs to know who a player is.

Three rules from `docs/collectors.md`, all load-bearing:

**`resolve_many` chunks internally at `BATCH_LIMIT` (500).** No caller-side
batching, deliberately — a second chunker is a second place for the limit to
drift from `player-identity`'s own `MAX_BATCH_QUERIES`, which the repo-root
`tests/test_identity_batch_limit.py` pins.

**`resolved.get(query)`, never `resolved.get(query, raw_id)`.** The default is
the whole non-negotiable. A defaulting `.get` is positionally safe — the code
runs, the types line up, every test stays green — and it adopts the upstream's
own GSIS key for exactly the players the authoritative service refused to
name. Those ids would then be published in `key_absences` as though they were
canonical, and a generator joining on them would silently match nothing.

**A failed request is not a refusal.** `resolve_many` catches per chunk and
records `query -> reason` on `IdentityClient.failures`, so a `player-identity`
outage is absent from the returned dict in precisely the same way a genuine
`resolved: false` is. Read only the dict and a total outage reports itself as
a week in which nobody was hurt.

--------------------------------------------------------------------------
`position` is sanitised before it is sent, and that is not defensive
--------------------------------------------------------------------------

`player-identity`'s `build_query` raises `HTTPException(422)` for a position
outside `KNOWN_POSITIONS`, and `resolve_queries` calls it **inside** the loop
over the batch — so one unmapped position code fails the whole 500-query
request, not one row of it. `IdentityClient` records the 422 as an
`identity_upstream_error` for all 500 and moves on, and the pass silently
loses every one of them.

Audited live against this collector's own upstreams for this build.
`players.csv` publishes 25 distinct position codes, of which exactly one —
`SAF`, 345 players — is absent from `KNOWN_POSITIONS` (which carries `S`, `FS`
and `SS` but not `SAF`). **No further unmapped code was found**, in either
`players.csv` or `injuries_2025.csv`; the injury feed publishes 16 codes and
all 16 are known.

`SAF` is a safety, so this collector's front filter already excludes it, and
the eight front codes it does send (`DE`, `DL`, `DT`, `ILB`, `LB`, `MLB`,
`NT`, `OLB`) are all in `KNOWN_POSITIONS`. That safety is a *consequence* of
the front filter, though, and would survive exactly until somebody widened it.
`SENDABLE_POSITIONS` makes it an assertion instead: an unrecognised code
travels as `None`, costing one scoring signal for one query, rather than as a
422 costing 500 resolutions. `defense-vs-position` guarded its own side the
same way and deliberately did not touch the shared library; the fix belongs in
`player-identity`'s batch loop, and is reported rather than reached into from
here.
"""

import os
from dataclasses import dataclass, field

import httpx
from collector_core.identity import IdentityClient, ResolveQuery

from .injuries import Absence
from .players import PlayerRef

# The crosswalk source both nflverse feeds are keyed by. Tier 1 in
# `player-identity`'s ladder: adopted exactly, never scored.
UPSTREAM_SOURCE = "gsis"

PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"

IDENTITY_UNAVAILABLE = "identity_unavailable"
IDENTITY_UPSTREAM_ERROR = "identity_upstream_error"
IDENTITY_UNRESOLVED = "identity_unresolved"

# The front position codes this collector will send. A mirror of the subset of
# `player-identity`'s `KNOWN_POSITIONS` that `players.FRONT_POSITION_GROUPS`
# can produce, written out rather than imported because collector-core cannot
# depend on a service and neither can this. See the module docstring for what
# an unlisted code costs if it is sent anyway.
SENDABLE_POSITIONS = frozenset({"DE", "DL", "DT", "ILB", "LB", "MLB", "NT", "OLB"})

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
    ever describes the last one. Summarised into **one** coverage error per
    pass: a per-row entry would fill `CoverageAccumulator`'s 50-entry cap by
    itself and push every other reason off the list.
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
        return f"{self.rows} absent player(s) unresolved: {reasons}"


def build_identity_client(client: httpx.AsyncClient) -> IdentityClient:
    """The `player-identity` seam, or `IdentityUnavailable`.

    Raises rather than returning a stub resolver. Every stub in this fleet
    mints an `fdy-` id from a hash of the upstream key; here that would
    publish ids `player-identity` never issued into `key_absences`, where a
    generator would join on them and match nothing — a total join failure
    wearing a complete-looking field.
    """
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if not base_url:
        raise IdentityUnavailable()
    token = os.getenv("COLLECTOR_TOKEN", "").strip()
    return IdentityClient(base_url, client, token=token or None)


def build_query(absence: Absence, reference: PlayerRef, season: int) -> ResolveQuery:
    """One absent front starter as a resolve query.

    `source`/`source_id` are what earn the Tier-1 adoption. `team`, `position`,
    `jersey_number` and `season` travel with them so `player-identity` can
    reject a crosswalk hit that contradicts the row it came from —
    `jersey_number` alone carries 0.20 of the server-side weighting.

    `team` comes from the **injury report**, not the roster feed:
    `players.csv`'s `latest_team` is a career-latest field and would name the
    wrong club for anyone traded mid-season. `position` comes from the roster
    feed, which publishes the finer code (`NT`, `OLB`) where the injury report
    collapses to `DT`/`LB`.

    No `name`. A GSIS id absent from the crosswalk would otherwise fall
    through to attribute scoring, and a feed that already carries a league id
    and is matched by name anyway is how two players with one name become one
    player.
    """
    position = reference.position
    return ResolveQuery(
        team=absence.team,
        position=position if position in SENDABLE_POSITIONS else None,
        jersey_number=reference.jersey_number,
        season=season,
        source=UPSTREAM_SOURCE,
        source_id=absence.gsis_id,
    )


async def resolve_absences(
    absences: list[Absence],
    *,
    season: int,
    front: dict[str, PlayerRef],
    identity: IdentityClient,
    failures: IdentityFailures,
) -> tuple[dict[str, list[str]], int]:
    """`team -> sorted canonical ids`, plus how many were not resolved.

    Only front players are queried: `key_absences` is a front field, and a
    hamstrung wide receiver is not an absence from a defensive front.

    Every id `player-identity` did not return is counted and reported rather
    than skipped — a skipped refusal shrinks nothing visible and reads as a
    quiet week on the injury report.

    No caller-side chunking: `resolve_many` does it at `BATCH_LIMIT`.
    """
    queries = [
        (absence, build_query(absence, front[absence.gsis_id], season))
        for absence in sorted(absences, key=lambda a: (a.team, a.gsis_id))
        if absence.gsis_id in front
    ]
    if not queries:
        return {}, 0

    resolved = await identity.resolve_many([query for _absence, query in queries])
    # Read immediately: the attribute describes the call that just returned.
    failures.record(identity.failures)

    by_team: dict[str, list[str]] = {}
    unresolved = 0
    for absence, query in queries:
        # `.get(query)`. No default — see the module docstring.
        canonical = resolved.get(query)
        if canonical is None:
            unresolved += 1
            continue
        by_team.setdefault(absence.team, []).append(canonical)
    return {team: sorted(set(ids)) for team, ids in by_team.items()}, unresolved

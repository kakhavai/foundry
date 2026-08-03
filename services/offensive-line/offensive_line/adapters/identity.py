"""Resolving identified starters to canonical `fdy-` ids.

Identity is used for **one field**: `starter_id`. Every unit column is keyed
by team, so nothing else here needs to know who a player is — which is why an
identity outage costs the starter rows and never the pass.

Three rules from `docs/collectors.md`, all load-bearing:

**`resolve_many` chunks internally at `BATCH_LIMIT` (500).** No caller-side
batching, deliberately — a second chunker is a second place for the limit to
drift from `player-identity`'s own `MAX_BATCH_QUERIES`, which the repo-root
`tests/test_identity_batch_limit.py` pins.

**`resolved.get(query)`, never `resolved.get(query, raw_id)`.** The default is
the whole non-negotiable. A defaulting `.get` is positionally safe — the code
runs, the types line up, every test stays green — and it adopts nflverse's own
GSIS key for exactly the players the authoritative service refused to name.
Those ids would be published as `starter_id` as though they were canonical,
and a generator joining on them would silently match nothing.

**A failed request is not a refusal.** `resolve_many` catches per chunk and
records `query -> reason` on `IdentityClient.failures`, so a `player-identity`
outage is absent from the returned dict in precisely the same way a genuine
`resolved: false` is. Read only the dict and a total outage reports itself as
a week in which no team could field five linemen.

--------------------------------------------------------------------------
`position` is sanitised before it is sent, and that is not defensive
--------------------------------------------------------------------------

`player-identity`'s `build_query` raises `HTTPException(422)` for a position
outside `KNOWN_POSITIONS`, and `resolve_queries` calls it **inside** the loop
over the batch — so one unmapped position code fails the whole 500-query
request, not one row of it. That is issue #106; the fix belongs in
`player-identity`'s batch loop and is deliberately not reached into from here,
the same way `defense-vs-position` and `defensive-front` guarded their own
side and left the shared service alone.

Audited live against this collector's own upstreams for this build:

| feed | column | codes | outside `KNOWN_POSITIONS` |
|---|---|---|---|
| `snap_counts` | `position` | `T`, `G`, `C`, `OL` | none |
| `players` | `position` (group `OL`) | `OT`, `G`, `C`, `OL` | none |
| `depth_charts` | `pos_abb` | `LT`,`LG`,`C`,`RG`,`RT` | **`LT`,`LG`,`RG`,`RT`** |

So the danger is real and it is specific: the codes this collector *thinks*
in — the spec's five-valued `starter_position` — are exactly the ones
`player-identity` does not know. Four of the five slot labels would 422 the
whole batch. `POSITION_FOR_SLOT` maps them onto the coarse codes the service
does carry (`T`/`G`/`C`), and `SENDABLE_POSITIONS` makes the guarantee an
assertion rather than a consequence: an unrecognised code travels as `None`,
costing one scoring signal for one query, rather than as a 422 costing 500
resolutions.
"""

import os
from dataclasses import dataclass, field

import httpx
from collector_core.identity import IdentityClient, ResolveQuery

from ..ratings import StarterSlot
from .players import PlayerRef

# The crosswalk source every nflverse feed here is keyed by. Tier 1 in
# `player-identity`'s ladder: adopted exactly, never scored.
UPSTREAM_SOURCE = "gsis"

PLAYER_IDENTITY_URL_ENV = "PLAYER_IDENTITY_URL"

IDENTITY_UNAVAILABLE = "identity_unavailable"
IDENTITY_UPSTREAM_ERROR = "identity_upstream_error"
IDENTITY_UNRESOLVED = "identity_unresolved"

# The spec's five slot labels, mapped onto the position codes
# `player-identity` actually knows. `LT`/`RT` are tackles, `LG`/`RG` guards.
# See the module docstring: sending the slot label itself 422s the batch.
POSITION_FOR_SLOT: dict[str, str] = {
    "LT": "T",
    "RT": "T",
    "LG": "G",
    "RG": "G",
    "C": "C",
}

# A mirror of the subset of `player-identity`'s `KNOWN_POSITIONS` this
# collector can produce, written out rather than imported because
# collector-core cannot depend on a service and neither can this. `OT`, `OG`
# and `OL` are included because `players.csv` publishes them for the same men
# `snap_counts` calls `T`, `G` and `OL`, and both feeds' codes can reach
# `build_query`.
SENDABLE_POSITIONS = frozenset({"C", "G", "OG", "OL", "OT", "T"})

MAX_FAILURE_DETAIL_CHARS = 200


class IdentityUnavailable(Exception):
    """No `player-identity` to talk to. Carries `.reason`."""

    def __init__(self, reason: str = IDENTITY_UNAVAILABLE) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class IdentityFailures:
    """Starters lost to a failed `player-identity` REQUEST, not a refusal.

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
        return f"{self.rows} starter(s) unresolved: {reasons}"


def build_identity_client(client: httpx.AsyncClient) -> IdentityClient:
    """The `player-identity` seam, or `IdentityUnavailable`.

    Raises rather than returning a stub resolver. Every stub in this fleet
    mints an `fdy-` id from a hash of the upstream key; here that would
    publish ids `player-identity` never issued as `starter_id`, where a
    generator would join on them and match nothing — a total join failure
    wearing a complete-looking field.
    """
    base_url = os.getenv(PLAYER_IDENTITY_URL_ENV, "").strip()
    if not base_url:
        raise IdentityUnavailable()
    token = os.getenv("COLLECTOR_TOKEN", "").strip()
    return IdentityClient(base_url, client, token=token or None)


def build_query(
    slot: StarterSlot,
    team: str,
    reference: PlayerRef | None,
    season: int,
) -> ResolveQuery:
    """One identified starter as a resolve query.

    `source`/`source_id` are what earn the Tier-1 adoption. `team`,
    `position`, `jersey_number` and `season` travel with them so
    `player-identity` can reject a crosswalk hit that contradicts the row it
    came from — `jersey_number` alone carries 0.20 of the server-side
    weighting.

    `team` comes from `snap_counts`, which is per game, never from
    `players.csv`'s `latest_team`: that is a career-latest field and names the
    wrong club for anyone traded mid-season.

    No `name`. A GSIS id absent from the crosswalk would otherwise fall
    through to attribute scoring, and a feed that already carries a league id
    and is matched by name anyway is how two linemen with one surname become
    one player.
    """
    position = POSITION_FOR_SLOT.get(slot.position)
    return ResolveQuery(
        team=team,
        position=position if position in SENDABLE_POSITIONS else None,
        jersey_number=reference.jersey_number if reference else None,
        season=season,
        source=UPSTREAM_SOURCE,
        source_id=slot.gsis_id,
    )


async def resolve_starters(
    slots: list[tuple[str, StarterSlot]],
    *,
    season: int,
    roster: dict[str, PlayerRef],
    identity: IdentityClient,
    failures: IdentityFailures,
) -> tuple[dict[str, str], int]:
    """`gsis_id -> canonical id`, plus how many were not resolved.

    Every id `player-identity` did not return is counted and reported rather
    than skipped — a skipped refusal shrinks nothing visible and reads as a
    quiet week, and here it would read as a team that could not field five
    linemen.

    No caller-side chunking: `resolve_many` does it at `BATCH_LIMIT`.
    """
    queries = [
        (slot.gsis_id, build_query(slot, team, roster.get(slot.gsis_id), season))
        for team, slot in sorted(slots, key=lambda item: (item[0], item[1].gsis_id))
    ]
    if not queries:
        return {}, 0

    resolved = await identity.resolve_many([query for _gsis_id, query in queries])
    # Read immediately: the attribute describes the call that just returned.
    failures.record(identity.failures)

    canonical: dict[str, str] = {}
    unresolved = 0
    for gsis_id, query in queries:
        # `.get(query)`. No default — see the module docstring.
        found = resolved.get(query)
        if found is None:
            unresolved += 1
            continue
        canonical[gsis_id] = found
    return canonical, unresolved

"""The `roster-scope` seam — the watchlist this collector's coverage is owed.

`player-stats` is scope-aware: the spec's `coverage.expected` is "one row per
`roster-scope` watchlist player whose team has completed its game for the
scoped week, plus any non-watchlist player who recorded at least one offensive
snap in those games". The first half of that sentence is this module.

**A fetch failure is not fatal and is not silent.** An empty watchlist would
shrink the expectation to whatever the box-score feed happened to return, which
is precisely the derive-expected-from-what-succeeded failure the coverage block
exists to catch. `fetch_watchlist` therefore returns the players it got *and*
the error entries explaining anything it did not, and `capture.py` floors the
expectation at `EXPECTED_FLOOR` regardless — so a `roster-scope` outage reads
as a low ratio, never as a healthy one.

`ROSTER_SCOPE_URL` ships **empty**, and that is a decision rather than an
oversight. `roster-scope`'s own `PLAYER_IDENTITY_URL` is empty too, so its
`player_id`s come from its stub resolver (a hash of `name|team|position`) while
this collector's come from ours (a hash of the GSIS id). The two id spaces do
not intersect, so narrowing to that watchlist today would report every
watchlist player as missing. Unscoped is the honest configuration until both
services point at a real `player-identity`; see `adapters/identity.py`.
"""

import os

import httpx

# Empty means "do not narrow" — see the module docstring for why that is the
# shipped default. Read at call time, not import time.
ROSTER_SCOPE_URL_ENV = "ROSTER_SCOPE_URL"

# A team defense has no box score, so it is not a row this collector can ever
# be owed. Excluding it here is what turns roster-scope's 416-slot universe
# into this collector's 384.
BOX_SCORE_ENTITY_TYPE = "player"

# Slots roster-scope is still fetching under its grace window count as owed:
# a player who left the depth chart on Tuesday still played on Sunday.
OWED_STATUSES: frozenset[str] = frozenset({"active", "grace"})


async def fetch_watchlist(
    client: httpx.AsyncClient,
) -> tuple[frozenset[str], list[dict]]:
    """The canonical `player_id`s this week's capture is owed a row for.

    Returns `(watchlist, errors)`. An empty watchlist with no errors means
    narrowing is switched off (`ROSTER_SCOPE_URL` unset); an empty watchlist
    *with* an error means `roster-scope` could not be reached, which
    `capture.py` records rather than treating as "nothing was owed".
    """
    base_url = os.getenv(ROSTER_SCOPE_URL_ENV, "").strip()
    if not base_url:
        return frozenset(), []

    headers = {}
    token = os.getenv("COLLECTOR_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = await client.get(
            f"{base_url.rstrip('/')}/scope/players", headers=headers
        )
        response.raise_for_status()
        players = response.json().get("players") or []
    except Exception as exc:  # noqa: BLE001 — classified, recorded, not fatal
        return frozenset(), [{"reason": "scope_unavailable", "detail": str(exc)}]

    watchlist = {
        str(row["player_id"])
        for row in players
        if row.get("entity_type", BOX_SCORE_ENTITY_TYPE) == BOX_SCORE_ENTITY_TYPE
        and row.get("membership_status") in OWED_STATUSES
        and row.get("player_id")
    }
    return frozenset(watchlist), []

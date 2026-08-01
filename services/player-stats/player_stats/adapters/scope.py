"""The `roster-scope` seam — the watchlist this collector's coverage is owed.

Read from the **lake**, not from `roster-scope` over HTTP. That is decision 5
of the narrowing design: the lake is append-only and already carries every
scope capture, so the last good scope survives a `roster-scope` outage — a
`roster-scope` outage costs this collector's coverage *freshness*, never the
availability of `/signals`. Reaching the service directly would make one
collector's downtime a fleet-wide stop.

This module used to reach `roster-scope` over HTTP, gated by an env var that
shipped empty: `roster-scope` minted `player_id`s from its own stub resolver
(a hash of `name|team|position`) while this collector minted them from the
GSIS crosswalk stub, so the two id spaces did not intersect and narrowing
would have reported every watchlist player as missing. Both collectors now
resolve through the real `player-identity`, the blocker is gone, and the HTTP
path — which contradicted the lake-read decision even before that — is
deleted along with the env var that gated it.

**Raises rather than returning empty.** An empty watchlist would shrink
`coverage.expected` to whatever the box-score feed happened to return, which
reports a truncated upstream as perfect — `Coverage.ratio` reads `1.0` when
`expected` is 0. `capture.py` still floors the expectation at `EXPECTED_FLOOR`
regardless, independent of whatever this returns.
"""

from collector_core.lake import LakeWriter
from collector_core.scope import ScopeClient

# The scope list this collector narrows to. Membership only — box scores are
# an offensive-production feed, so the matchup list (`injury-report`'s other
# half) names nobody this collector has a row for.
SCOPE_SIGNAL_TYPE = "scope_membership_weekly"

# A team defense has no box score, so it is not a row this collector can ever
# be owed. This is what turns roster-scope's 416-slot universe into this
# collector's 384. Verified against `roster_scope.scope.resolve_membership`'s
# own minting (`f"fdy-dst-{team.lower()}"`) rather than assumed.
TEAM_DEFENSE_PREFIX = "fdy-dst-"


async def fetch_watchlist(lake: LakeWriter, season: int, week: int) -> frozenset[str]:
    """The canonical `player_id`s this week's capture is owed a row for.

    Raises `ScopeUnavailable` when there is no usable scope; the caller must
    treat that as zero upstream calls and a `present: 0` envelope — see
    `capture.py`. `ScopeClient` already falls back to `week - 1` and already
    drops `excluded` rows while keeping `grace` ones, so a player who left the
    depth chart on Tuesday still played on Sunday; this module does not
    reimplement either.
    """
    scope = await ScopeClient(lake).fetch(SCOPE_SIGNAL_TYPE, season, week)
    return frozenset(
        player_id
        for player_id in scope.members
        if not player_id.startswith(TEAM_DEFENSE_PREFIX)
    )

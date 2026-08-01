"""Narrowing to membership UNION matchup.

This collector's registry entry used to argue *against* narrowing: "an
opposing cornerback ruled out moves a receiver's projection as much as the
receiver's own hamstring does, and defenders never appear on an
offence-oriented watchlist at all." That is a correct observation and the
wrong conclusion -- it is an argument for reading the matchup list TOO, not an
argument for fetching all ~1,700 players in the league. `roster-scope`
publishes a separately-bounded ~608-slot CB/S/LB/DL/OL universe for exactly
this reason, and `ScopeClient.fetch_union` is what turns two lists into the
one set this collector actually needs.

**All-or-nothing on purpose.** A present membership list with an absent
matchup list would narrow to offence alone and silently drop every
defender -- which looks exactly like a working narrow, not like a degraded
one. `fetch_union` raises rather than returning the half that resolved, so a
missing matchup list fails the whole pass closed instead of quietly halving
the signal.

**Only `player_injury_status` is filtered against this scope.**
`team_injury_report` is keyed by team, not by player, and answers "did this
club file a report at all" -- a question this collector owes an answer to for
every scheduled club regardless of which of its players happen to be in
scope. Narrowing that signal type by player membership would silently drop a
club's filing (and the `report_not_published` coverage tracking that depends
on it) whenever every player it listed happened to be out of scope, which is
exactly the "looks like a working narrow" failure this module exists to avoid
one layer up. See `capture.py` for where the two signal types diverge.
"""

from collector_core.scope import Scope, ScopeClient

# The two lists that make up this collector's narrowed universe. Both are
# read from the lake -- never from `roster-scope` over HTTP -- so the last
# good union survives a `roster-scope` outage; see `ScopeClient`'s own
# docstring for why the lake is the seam every collector reads through.
SCOPE_SIGNAL_TYPES = ("scope_membership_weekly", "scope_matchup_weekly")


async def fetch_scope(lake, season: int, week: int) -> Scope:
    """Every player this collector may publish `player_injury_status` for,
    offence and defence alike. Raises `ScopeUnavailable` if either list is
    missing -- the caller must treat that as zero upstream calls and a
    `present: 0` envelope for every signal type, never an unnarrowed
    fallback."""
    return await ScopeClient(lake).fetch_union(SCOPE_SIGNAL_TYPES, season, week)

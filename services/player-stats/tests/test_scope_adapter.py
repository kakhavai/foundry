"""The `roster-scope` seam, read from the lake.

`test_coverage_floor.py` owns how the watchlist feeds `coverage.expected`;
this file owns the adapter in isolation — the lake read, the raise-not-empty
contract, and the team-defense filter that turns roster-scope's 416-slot
universe into this collector's 384.
"""

import pytest
from collector_core.scope import ScopeUnavailable

from player_stats.adapters.scope import TEAM_DEFENSE_PREFIX, fetch_watchlist

from .conftest import SEASON, WEEK, seed_scope


async def test_the_watchlist_comes_from_the_lake(lake):
    seed_scope(lake, ["fdy-a", "fdy-b"])
    assert await fetch_watchlist(lake, SEASON, WEEK) == frozenset({"fdy-a", "fdy-b"})


async def test_an_absent_scope_raises_rather_than_returning_empty(lake):
    """An empty watchlist would shrink `coverage.expected` to whatever the
    box-score feed happened to return — the derive-expected-from-what-
    succeeded failure the coverage block exists to catch."""
    with pytest.raises(ScopeUnavailable):
        await fetch_watchlist(lake, SEASON, WEEK)


async def test_a_team_defense_is_excluded_from_the_watchlist(lake):
    """A team defense has no box score, so it is not a row this collector can
    ever be owed. Verified against roster-scope's own minting: `fdy-dst-<team
    lowercased>` (see `roster_scope.scope.resolve_membership`)."""
    assert TEAM_DEFENSE_PREFIX == "fdy-dst-"
    seed_scope(lake, ["fdy-a", "fdy-dst-sf"])
    watchlist = await fetch_watchlist(lake, SEASON, WEEK)
    assert "fdy-dst-sf" not in watchlist
    assert watchlist == frozenset({"fdy-a"})


def test_roster_scope_url_is_gone():
    """The HTTP path contradicted decision 5 and must not come back."""
    import player_stats.adapters.scope as module

    assert not hasattr(module, "ROSTER_SCOPE_URL_ENV")
    assert "ROSTER_SCOPE_URL" not in module.__doc__

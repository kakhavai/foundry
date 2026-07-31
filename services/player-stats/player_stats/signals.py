"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.
"""

from collections.abc import Mapping

# The phase doc's `/signals` filters for this collector are `season`, `week`,
# `player_id` and `team`. `game_id` and `position` are added because both are
# first-class keys on every emitted row — a player traded mid-season yields one
# row per game, so `game_id` is how a consumer pins the one it means.
#
# Nothing here is declared that `signal_matches` does not implement: the router
# would accept it, the predicate would ignore it, and the response would return
# everything — which looks exactly like a working filter.
SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "player_id",
    "game_id",
    "team",
    "position",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("player_id", "game_id", "team", "position")


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**. Every field filtered here happens to be a string on the row as
    well, but `str()` on the row side is kept anyway: it costs nothing and it
    is the single most common bug in this function the day somebody adds an
    integer field to `ROW_FILTERS`.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True

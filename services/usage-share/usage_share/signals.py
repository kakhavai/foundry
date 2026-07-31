"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.
"""

from collections.abc import Mapping

# Exactly the fields `capture.build_signal` emits that a caller would narrow on.
# Nothing here is declared that `ROW_FILTERS` does not implement: the router
# would accept it, the predicate would ignore it, and the response would return
# everything — which looks precisely like a working filter.
#
# `position` is deliberately absent even though the adapter carries one: it is
# not a published field, so filtering on it would match no row at all. The
# phase doc's field table for this collector has no position column, and this
# collector does not add one.
SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "player_id",
    "team",
    "game_id",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("player_id", "team", "game_id")


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int. Comparing them directly
    makes `?week_index=3` silently match nothing. `str()` on the row side is
    the fix, and forgetting it is the single most common bug in this function.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True

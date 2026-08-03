"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.
"""

from collections.abc import Mapping

# `team_id` and `record_type` are the two cuts a consumer actually makes: one
# team's rows, or every unit row without the five-times-larger starter tail.
# `starter_position` is there because "who plays left tackle" is a question the
# generator asks directly.
#
# Nothing here filters on `lineup_hash`: it is an opaque join key, and a
# filter that only ever matches a value the caller already read off a row buys
# nothing a client-side comparison does not.
SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "team_id",
    "record_type",
    "starter_position",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("team_id", "record_type", "starter_position")


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int. Comparing them directly
    makes a numeric filter silently match nothing. `str()` on the row side is
    the fix, and forgetting it is the single most common bug in this function.

    A unit row carries no `starter_position`, so `?starter_position=LT`
    correctly excludes it — `str(None)` is `"None"`, which no caller sends.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True

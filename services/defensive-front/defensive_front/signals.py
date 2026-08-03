"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.
"""

from collections.abc import Mapping

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "team_id",
    "unit",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
#
# `unit` is declared even though `ratings.UNITS` currently holds one value. It
# is a real dimension of the row — the spec keys rows by `(team_id, unit)` —
# and a consumer written against `?unit=overall` must keep working on the day
# an alignment source appears and `interior`/`edge` join it. Declaring a
# filter that IS implemented is safe; the rule `docs/collectors.md` warns
# about is the reverse — a filter declared and not implemented returns
# everything and looks exactly like one that works.
ROW_FILTERS: tuple[str, ...] = ("team_id", "unit")


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

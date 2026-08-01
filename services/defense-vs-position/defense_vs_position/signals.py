"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal -- `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.

**Every filter below is implemented in `signal_matches`, and the list is
derived from `ROW_FILTERS` rather than written twice.** Declaring one the
predicate does not apply is the failure this shape prevents: the router would
accept it, the predicate would ignore it, and the response would return all
576 rows -- which looks exactly like a filter that matched everything.

The full row set is 576 rows (~150 KB), so this is a convenience rather than a
boundary; a caller may take the lot and slice it itself, the same way
`player-projections` treats `pos`.
"""

from collections.abc import Mapping

# The subset of `SUPPORTED_FILTERS` this module applies. The universal three
# are already applied by the router before a row reaches here.
#
# `rank_divergence_flagged` is deliberately filterable: it is how an operator
# answers "show me the rows the guard wants reviewed" without reading the
# errors array. Query values are strings, so it is compared as one.
ROW_FILTERS: tuple[str, ...] = (
    "team_id",
    "position",
    "alignment",
    "scoring_format",
    "rank_divergence_flagged",
)

SUPPORTED_FILTERS: tuple[str, ...] = ("season", "week", "signal_type", *ROW_FILTERS)


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may be an int or a bool. Comparing them
    directly makes `?games_sampled=3` silently match nothing; `str()` on the
    row side is the fix and forgetting it is the most common bug here.

    `rank_divergence_flagged` is a bool, so `str()` gives `"True"`/`"False"` --
    matched case-insensitively so `?rank_divergence_flagged=true` behaves the
    way every other HTTP API in this repo does.
    """
    for key in ROW_FILTERS:
        if key not in params:
            continue
        if str(row.get(key)).casefold() != params[key].casefold():
            return False
    return True

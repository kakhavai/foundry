"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.

**`week` is a scope filter, not a row filter, and on this collector that
distinction has teeth.** The router matches it against the envelope's scope —
the week the pass was captured for. It does NOT select revisions covering that
week, because a revision's rates are drawn from every week inside the
revision. Asking "which regime governed week 9" is `effective_from_week` /
`effective_to_week` arithmetic on the rows, or the
`GET /teams/{team_id}/revisions` route, which exists precisely because that
arithmetic is awkward to do against a filtered `/signals` response.
"""

from collections.abc import Mapping

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "team_id",
    "revision_id",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("team_id", "revision_id")


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int. Comparing them directly
    makes a numeric filter silently match nothing. `str()` on the row side is
    the fix, and forgetting it is the single most common bug in this function.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True

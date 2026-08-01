"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here beyond
those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as a
loud error, not look like a quiet week.

**Nothing is declared here that `signal_matches` does not implement.** The
router accepts whatever `SUPPORTED_FILTERS` names and hands the rest to this
module, so an unimplemented filter returns every row — which looks exactly like
a working filter that matched everything.
"""

from collections.abc import Mapping

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "player_id",
    "position",
    "position_age_curve_stage",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = (
    "player_id",
    "position",
    "position_age_curve_stage",
)


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int or None. Comparing them
    directly makes a numeric filter silently match nothing; `str()` on the row
    side is the fix.

    A filter naming a field this signal type does not carry excludes the row
    rather than passing it through. `player_draft_capital` rows have no
    `position`, so `?position=RB` returns biographical rows only — which is the
    honest answer to "rows where position is RB", not a bug. Passing them
    through instead would make a position filter look like it silently ignored
    three quarters of the response.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True

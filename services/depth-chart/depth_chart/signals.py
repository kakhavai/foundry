"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here beyond
those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as a
loud error, not look like a quiet week.
"""

from collections.abc import Mapping

# `team` and `position` are on BOTH signal types, so either narrows a mixed
# response consistently. `gsis_id` is on `team_depth_chart` only — it is the
# published crosswalk key, and it is offered instead of `player_id` because
# `player_id` is null on every row today (see `capture.build_chart_signal`).
# Declaring a `player_id` filter that matched only nulls would be exactly the
# "looks like a working filter" failure this docstring warns about.
SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "team",
    "position",
    "gsis_id",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("team", "position", "gsis_id")


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int or None. Comparing them
    directly makes a numeric filter silently match nothing; `str()` on the row
    side is the fix, and forgetting it is the single most common bug here.

    A `depth_chart_stability` row carries no `gsis_id`, so `?gsis_id=` narrows
    that envelope to nothing rather than returning all of it — which is the
    honest answer: those rows are per position group, not per player.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True

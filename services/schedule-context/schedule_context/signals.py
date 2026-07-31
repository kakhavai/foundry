"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.

schedule-context emits no `player_id` — its signals are keyed by game and
team — so accepting one would return every row for a query that asked about
one player.
"""

from collections.abc import Mapping

# `team` and `opponent` rather than `team_id`/`opponent_id`: the fleet's query
# vocabulary is `team` (weather uses it), and the row field it maps onto is
# named in `ROW_FILTERS` below so the two cannot drift apart silently.
SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "game_id",
    "team",
    "opponent",
)

# Query parameter -> the row field it filters on. The universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: dict[str, str] = {
    "game_id": "game_id",
    "team": "team_id",
    "opponent": "opponent_id",
}


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int. Comparing them directly
    makes `?week=3` silently match nothing. `str()` on the row side is the fix,
    and forgetting it is the single most common bug in this function.
    """
    for param, field in ROW_FILTERS.items():
        if param in params and str(row.get(field)) != params[param]:
            return False
    return True

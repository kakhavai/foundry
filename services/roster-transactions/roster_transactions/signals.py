"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as a
loud error, not look like a quiet week — which for *this* collector is a
genuinely common state, and therefore an especially convincing disguise.
"""

from collections.abc import Mapping

__all__ = ["ROW_FILTERS", "SUPPORTED_FILTERS", "signal_matches"]

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "player_id",
    "team",
    "transaction_type",
    "confidence",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here. Every entry has a
# branch below — declaring one without implementing it makes the router accept
# the parameter, the predicate ignore it, and the response return everything,
# which looks exactly like a working filter.
ROW_FILTERS: tuple[str, ...] = (
    "player_id",
    "team",
    "transaction_type",
    "confidence",
)


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int or `None`. Comparing them
    directly makes a numeric filter silently match nothing; `str()` on the row
    side is the fix, and forgetting it is the most common bug in this function.

    `team` matches **either side** of the move. A transaction is an edge
    between two rosters, and a consumer asking "what happened to KC this week"
    means arrivals and departures alike — matching only `to_team` would hide
    every player the team lost, which is the half that breaks a depth chart.
    """
    for key in ("player_id", "transaction_type", "confidence"):
        if key in params and str(row.get(key)) != params[key]:
            return False
    if "team" in params:
        wanted = params["team"]
        if wanted not in {str(row.get("from_team")), str(row.get("to_team"))}:
            return False
    return True

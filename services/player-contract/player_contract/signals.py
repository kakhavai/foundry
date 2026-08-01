"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as a
loud error, not look like a quiet week.

`is_contract_year` is the query this collector exists to answer — "who is
playing out the final season of a deal" — so it is a filter rather than
something every caller re-implements over 384 rows.
"""

from collections.abc import Mapping

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "player_id",
    "team",
    "is_contract_year",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("player_id", "team", "is_contract_year")

# Query-string spellings of `true`. Anything else is `false` — including
# nonsense, which therefore matches the `False` rows rather than 422ing. That is
# the deliberate trade: the router owns parameter *names*, and adding per-value
# validation here would put a second, divergent 422 policy in a second place.
TRUE_VALUES = frozenset({"true", "1", "yes", "t"})


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int or a bool. Comparing them
    directly makes `?week_index=3` silently match nothing, and `str()` on the
    row side is the usual fix.

    **A bool needs more than `str()`.** `str(True)` is `"True"`, and no client
    sends `?is_contract_year=True` with a capital T — so the naive form matches
    nothing for every value a caller would actually type, while looking exactly
    like a working filter. `is_contract_year` is additionally **tri-state**: a
    row whose contract term the upstream did not supply carries `None`, and such
    a row must match neither `true` nor `false`, because "we do not know" is not
    an answer to either question.
    """
    for key in ROW_FILTERS:
        if key not in params:
            continue
        value = row.get(key)
        if isinstance(value, bool):
            if value is not (params[key].strip().lower() in TRUE_VALUES):
                return False
        elif value is None:
            # An absent or unknown field matches nothing that was asked for. The
            # `str(None) == "None"` comparison below would otherwise let
            # `?team=None` select every row whose club the feed left ambiguous.
            return False
        elif str(value) != params[key]:
            return False
    return True

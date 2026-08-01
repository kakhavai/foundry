"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.

`crew_id` and `game_id` are the two a consumer actually slices by — "what did
this crew do" and "who is calling this game". Both appear on
`game_crew_assignment`; only `crew_id` appears on `crew_tendency_rates`, which
is not keyed by game at all. That asymmetry is why `signal_matches` tests
membership rather than presence: filtering rates by `game_id` must return
nothing rather than everything.
"""

from collections.abc import Mapping

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "crew_id",
    "game_id",
    "referee_name",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
ROW_FILTERS: tuple[str, ...] = ("crew_id", "game_id", "referee_name")


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int. Comparing them directly
    makes a numeric filter silently match nothing; `str()` on the row side is
    the fix.

    A row that does not carry the filtered field at all does **not** match. A
    `crew_tendency_rates` row has no `game_id`, and treating an absent field as
    a wildcard would return every crew's rates for `?game_id=2025_01_DAL_PHI`
    — an answer that looks like data and is not.
    """
    for key in ROW_FILTERS:
        if key not in params:
            continue
        if key not in row or str(row[key]) != params[key]:
            return False
    return True

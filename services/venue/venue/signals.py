"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.

**`venue`'s two signal types have different row shapes**, so each filter has to
say what it means on both. `venue_id` is on both. `game_id` exists only on an
assignment row. `team` means "a tenant of this venue" on a static row and "the
designated home club" on an assignment row — deliberately the same parameter,
because a caller asking `?team=NYJ` wants the Jets' building and the Jets' home
games, and making them ask twice with two names would be an implementation
detail leaking into the contract.
"""

from collections.abc import Mapping

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "venue_id",
    "game_id",
    "team",
)


def _matches_venue_id(row: dict, value: str) -> bool:
    return str(row.get("venue_id")) == value


def _matches_game_id(row: dict, value: str) -> bool:
    """Only an assignment row has a `game_id`.

    A static row is therefore excluded by `?game_id=...` rather than passed
    through. Passing it through would make a game-scoped query return all
    thirty venue records alongside the one game, which reads as a filter that
    does not work.
    """
    return str(row.get("game_id")) == value


def _matches_team(row: dict, value: str) -> bool:
    """A tenant on a static row; the designated home club on an assignment row.

    Both are checked and either satisfies, because the two row shapes carry the
    concept under different names and a row has only one of them.
    """
    if "home_team_ids" in row:
        return value in {str(team) for team in row.get("home_team_ids") or ()}
    return str(row.get("designated_home_team_id")) == value


# The subset of SUPPORTED_FILTERS this module applies: the universal three are
# already applied by the router before a row reaches here. Declared as a mapping
# rather than a tuple so a filter cannot be listed above without a predicate —
# the router would accept it, this function would ignore it, and the response
# would return everything, which looks exactly like a working filter.
ROW_FILTERS = {
    "venue_id": _matches_venue_id,
    "game_id": _matches_game_id,
    "team": _matches_team,
}


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may well be an int or a bool. Comparing them
    directly makes `?week=3` silently match nothing; `str()` on the row side is
    the fix, and forgetting it is the single most common bug in this function.
    """
    for key, predicate in ROW_FILTERS.items():
        if key in params and not predicate(row, params[key]):
            return False
    return True

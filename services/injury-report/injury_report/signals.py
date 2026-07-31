"""Which `/signals` query parameters this collector accepts, and what they mean.

`season`, `week` and `signal_type` are universal — `collector_core.routes`
applies those itself against each envelope's scope. Everything named here
beyond those three is row-level filtering this collector owns.

A parameter that is neither universal nor listed in `SUPPORTED_FILTERS` is
rejected with 422 rather than silently ignored: a client bug should surface as
a loud error, not look like a quiet week.

**`team` is a convenience, not a boundary, and that matters more here than
anywhere else in the fleet.** `team_injury_report` carries every scheduled
club's filing regardless of the roster scope — it is keyed by team, not by
player, and answers "did this club file" for all of them. A caller that
filters `team_injury_report` to its own club has thrown away the rest of the
league's filings, and one filtering `player_injury_status` to its own club has
thrown away the opponent-side rows: `capture.py` narrows that signal to
`roster-scope`'s membership UNION matchup list, not to any one team, because
an opposing cornerback being ruled out changes a receiver's projection as much
as the receiver's own hamstring does.
"""

from collections.abc import Mapping

SUPPORTED_FILTERS: tuple[str, ...] = (
    "season",
    "week",
    "signal_type",
    "team",
    "player_id",
    "game_id",
    "practice_day",
    "game_designation",
)

# The subset of the above this module filters on: the universal three are
# already applied by the router before a row reaches here.
#
# Every one is implemented below. Declaring a filter that `signal_matches`
# ignores is worse than not declaring it: the router accepts the parameter, the
# predicate returns everything, and the response looks exactly like a working
# filter.
ROW_FILTERS: tuple[str, ...] = (
    "team",
    "player_id",
    "game_id",
    "practice_day",
    "game_designation",
)


def signal_matches(row: dict, params: Mapping[str, str]) -> bool:
    """Whether one signal row satisfies the collector-specific query params.

    `params` values arrive from the query string and are therefore **always
    strings**, while a row's value may be an int, a bool or `None`. Comparing
    them directly makes a filter silently match nothing, so the row side is
    `str()`-ed.

    Two consequences worth stating rather than discovering:

    * The two signal types carry different fields. `team_injury_report` rows
      have no `player_id`, so `?player_id=` narrows that envelope to nothing —
      which is correct, not a bug: no club-level row describes one player.
    * `?game_designation=none` matches a player published as carrying no
      designation. It does **not** match a player for whom nothing has been
      published yet, whose `game_designation` is `null`. That is the
      distinction this collector exists to preserve, and collapsing it in the
      filter would undo it at the last possible moment.
    """
    for key in ROW_FILTERS:
        if key in params and str(row.get(key)) != params[key]:
            return False
    return True

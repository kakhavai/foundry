"""Kickoff instant -> broadcast window, and the slate arithmetic around it.

Pure functions over already-parsed values. Nothing here touches the network,
the lake or the clock, which is what makes the two guards below testable
without a fixture server.

--------------------------------------------------------------------------
The enum, and the one value added to it
--------------------------------------------------------------------------

The spec's `window_id` enum is `intl_early`, `sun_early`, `sun_late`, `snf`,
`mnf`, `tnf`, `sat_special`, `holiday`, `playoff`. Against the real feed that
enum has a hole: the 2025 season opens with a Friday 20:00 game in Sao Paulo
and the 2026 season opens with a **Wednesday** 20:20 game, and 2026 carries a
second Wednesday game the night before Thanksgiving. None of those is a
Saturday, a holiday, or a playoff.

`weeknight_special` is therefore added, and it is a **disclosed deviation**
(see the README). The alternative was to leave those games with a null
`window_id`, which under this collector's own coverage rule makes them a
permanent, unfixable coverage miss and — because an unslotted game could
belong to any slot — nulls `games_in_window` for every other game in the same
week. One added enum value is a far smaller lie than that.

--------------------------------------------------------------------------
`is_primetime` is computed from the EASTERN clock, deliberately
--------------------------------------------------------------------------

The spec defines `is_primetime` as "kickoff **local** time at or after 20:00"
and `kickoff_local_time` as the "venue wall clock". The feed's `gametime` is
Eastern for every game. Reading the spec literally would make a 20:20 ET
Thursday kickoff in Los Angeles a 17:20 local kickoff and therefore *not*
primetime, which is not what anyone means by primetime — the NFL sells and
schedules its primetime packages on the Eastern clock. So the threshold is
applied to Eastern time, and `kickoff_local_time` is null with a reason
rather than silently redefined. The full argument is in the README.

Consequence worth knowing: a 19:00 or 19:15 ET Monday game (the second half
of an MNF doubleheader week) is `is_primetime: false` under the spec's own
20:00 threshold while sitting in the `mnf` window. A consumer that means
"national primetime package" should read `window_id`, not `is_primetime`.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

__all__ = [
    "MIN_REG_SEASON_WEEK_GAMES",
    "PRIMETIME_START",
    "PRIMETIME_WINDOWS",
    "REGULAR_SEASON",
    "SLATE_BELOW_MINIMUM_GAMES",
    "SLATE_UNSLOTTED_GAME",
    "SLATE_WEEK_SHRANK",
    "WINDOW_IDS",
    "Slate",
    "build_slate",
    "classify_window",
    "is_league_holiday",
    "is_primetime",
    "thanksgiving",
]

WINDOW_INTL_EARLY = "intl_early"
WINDOW_SUN_EARLY = "sun_early"
WINDOW_SUN_LATE = "sun_late"
WINDOW_SNF = "snf"
WINDOW_MNF = "mnf"
WINDOW_TNF = "tnf"
WINDOW_SAT_SPECIAL = "sat_special"
WINDOW_HOLIDAY = "holiday"
WINDOW_PLAYOFF = "playoff"
# Added to the spec's enum. See the module docstring and the README.
WINDOW_WEEKNIGHT_SPECIAL = "weeknight_special"

WINDOW_IDS: tuple[str, ...] = (
    WINDOW_INTL_EARLY,
    WINDOW_SUN_EARLY,
    WINDOW_SUN_LATE,
    WINDOW_SNF,
    WINDOW_MNF,
    WINDOW_TNF,
    WINDOW_SAT_SPECIAL,
    WINDOW_HOLIDAY,
    WINDOW_PLAYOFF,
    WINDOW_WEEKNIGHT_SPECIAL,
)

# The three national primetime packages, and the only windows flex scheduling
# moves a game into or out of. Used to tell `flexed_in` from `flexed_out`;
# `sat_special`, `holiday` and `weeknight_special` are standalone slots but
# they are fixtures of the calendar rather than flex destinations.
PRIMETIME_WINDOWS = frozenset({WINDOW_SNF, WINDOW_MNF, WINDOW_TNF})

# The spec's own threshold, applied to the Eastern clock. See the docstring.
PRIMETIME_START = time(20, 0)

# Anything before noon Eastern is the international morning window. Keyed on
# the SLOT rather than on the `location` column: the feed marks a designated
# home game in London as `location: Home`, and its `stadium` column names the
# US building the game was moved out of, so neither identifies the window.
INTERNATIONAL_BEFORE = time(12, 0)

REGULAR_SEASON = "REG"

# The fewest regular-season games a week can hold. Six teams on bye is the
# league maximum, so 32 teams produce at least 13 games. Measured across
# 2024-2026 the observed minimum is exactly 13. A week listing fewer than
# this has been truncated somewhere between the publisher and here, and every
# `games_in_window` derived from it would be an undercount.
MIN_REG_SEASON_WEEK_GAMES = 13

# Why a week's slot counts were withheld. Three findings, three different
# operator responses: wait (the league has not slotted it yet), investigate the
# feed, or investigate the transport.
SLATE_UNSLOTTED_GAME = "unslotted_game"
SLATE_BELOW_MINIMUM_GAMES = "below_minimum_games"
SLATE_WEEK_SHRANK = "week_shrank"

_MONDAY, _THURSDAY, _SATURDAY, _SUNDAY = 0, 3, 5, 6


def thanksgiving(year: int) -> date:
    """The fourth Thursday of November."""
    first = date(year, 11, 1)
    first_thursday = first + timedelta(days=(_THURSDAY - first.weekday()) % 7)
    return first_thursday + timedelta(days=21)


def is_league_holiday(day: date) -> bool:
    """Thanksgiving, the Friday after it, or Christmas Day.

    These three dates carry standalone national games that are not part of any
    weekly package: the Thanksgiving triple-header, the Black Friday game, and
    the Christmas slate. Classifying them by weekday instead would file
    Thanksgiving as `tnf` and a Christmas-on-Friday game as a weeknight
    special, both of which lose the fact that made the slot worth a name.
    """
    if (day.month, day.day) == (12, 25):
        return True
    turkey_day = thanksgiving(day.year)
    return day in (turkey_day, turkey_day + timedelta(days=1))


def classify_window(*, kickoff_eastern: datetime | None, game_type: str) -> str | None:
    """The broadcast window this kickoff sits in, or `None` if unassigned.

    `None` is a real answer, not a failure: a game the feed lists without a
    kickoff time has no window yet. The caller records it as a coverage miss.
    **It must never fall through to a plausible default** — the spec names
    "defaulting to `sun_early`" as the silent failure this collector exists to
    avoid.
    """
    if game_type != REGULAR_SEASON:
        # WC / DIV / CON / SB. Preseason never reaches here (the adapter drops
        # it), so every non-REG game is a postseason game.
        return WINDOW_PLAYOFF
    if kickoff_eastern is None:
        return None

    day = kickoff_eastern.date()
    if is_league_holiday(day):
        return WINDOW_HOLIDAY

    clock = kickoff_eastern.time()
    if clock < INTERNATIONAL_BEFORE:
        return WINDOW_INTL_EARLY

    weekday = day.weekday()
    if weekday == _SUNDAY:
        if clock < time(16, 0):
            return WINDOW_SUN_EARLY
        if clock < PRIMETIME_START:
            return WINDOW_SUN_LATE
        return WINDOW_SNF
    if weekday == _MONDAY:
        return WINDOW_MNF
    if weekday == _THURSDAY:
        return WINDOW_TNF
    if weekday == _SATURDAY:
        return WINDOW_SAT_SPECIAL
    return WINDOW_WEEKNIGHT_SPECIAL


def is_primetime(kickoff_eastern: datetime | None) -> bool | None:
    """The spec's `>= 20:00` threshold, on the Eastern clock.

    `None` when the kickoff time is unpublished — `False` would claim the game
    is not primetime, which is a fact this pass does not have.
    """
    if kickoff_eastern is None:
        return None
    return kickoff_eastern.time() >= PRIMETIME_START


@dataclass(frozen=True)
class Slate:
    """How many games share each kickoff instant, and which weeks are unsafe.

    `counts` is keyed by the **exact kickoff instant**, not by `window_id`.
    That distinction is load-bearing for `is_standalone`: the four Divisional
    Round games all carry `window_id: playoff`, and counting by window would
    report `games_in_window: 4` for four games that are each the only football
    on television when they kick off. Counting by instant reports 1 for each,
    which is what "kicking off in the same slot" means.

    `incomplete_weeks` names the weeks whose counts must **not** be published.
    The spec's trap, stated: `games_in_window` is a derived count over a whole
    slate, so a partial fetch produces a wrong value for **every** game in the
    window rather than only the missing one, and `is_standalone` inherits it.
    An undercount is also the dangerous direction — it manufactures standalone
    primetime games that do not exist.
    """

    counts: dict[datetime, int]
    incomplete_weeks: frozenset[int]
    # `week -> why`, so an operator can tell a late-season TBD game from a
    # truncated document. Three different findings with three different
    # responses; one shared `incomplete_slate` string would erase that.
    incomplete_reasons: dict[int, str]

    def games_in_window(self, game) -> int | None:
        """The slot count for one game, or `None` when it cannot be trusted."""
        if game.week in self.incomplete_weeks or game.kickoff_at is None:
            return None
        return self.counts.get(game.kickoff_at)


def build_slate(
    games: Iterable, *, week_high_water: dict[int, int] | None = None
) -> Slate:
    """Slot counts plus the weeks whose slate cannot be shown to be complete.

    A week is unsafe on any of **three** independent findings, and they catch
    different failures:

    * **any game in it has no kickoff instant** (`unslotted_game`) — that game
      could belong to any slot in the week, so every slot in the week is a
      potential undercount. The ordinary late-season "flex TBD" case.
    * **a regular-season week lists fewer than `MIN_REG_SEASON_WEEK_GAMES`
      games** (`below_minimum_games`) — the league cannot schedule fewer, so
      the document was truncated between the publisher and here.
    * **a week holds fewer games than this partition has ever seen it hold**
      (`week_shrank`) — the arm above is a *floor*, not a completeness proof,
      and a stream truncated mid-document leaves the tail week with 13-15
      games that all have kickoffs. Reproduced: a 14-game week that lost one
      of its two 16:25 games published `games_in_window: 1`,
      `is_standalone: true`, `distribution: national` for the survivor, with
      no `incomplete_slate` anywhere — "the dangerous direction" this module
      exists to prevent, arriving through the gap between the first two arms.

    `week_high_water` is a **high-water mark** over every snapshot read, not
    the previous pass's count (`History.week_high_water`). Comparing against
    the previous count detects the *edge* and not the *state*: 16 -> 14 flags
    and 14 -> 14 goes quiet, so a truncation that persists is announced for
    one pass and then republishes wrong counts indefinitely. Its cost is that
    a week which legitimately shrinks latches as incomplete — the withhold
    direction, and self-healing once the high snapshot ages out of
    `MAX_HISTORY_SNAPSHOTS`.

    It is empty on a first capture, which makes the third arm inert rather
    than a guess — a collector with no baseline must not invent one.

    All three are computed over the games the feed LISTED, never over the rows
    that were successfully built — deriving completeness from success would
    make a total mapping failure read as a complete slate of zero games.
    """
    week_high_water = week_high_water or {}
    counts = Counter(game.kickoff_at for game in games if game.kickoff_at is not None)

    by_week: dict[int, list] = defaultdict(list)
    for game in games:
        by_week[game.week].append(game)

    reasons: dict[int, str] = {}
    for week, week_games in by_week.items():
        if any(game.kickoff_at is None for game in week_games):
            reasons[week] = SLATE_UNSLOTTED_GAME
            continue
        regular = [g for g in week_games if g.game_type == REGULAR_SEASON]
        if regular and len(regular) < MIN_REG_SEASON_WEEK_GAMES:
            reasons[week] = SLATE_BELOW_MINIMUM_GAMES
            continue
        if len(week_games) < week_high_water.get(week, 0):
            reasons[week] = SLATE_WEEK_SHRANK

    return Slate(
        counts=dict(counts),
        incomplete_weeks=frozenset(reasons),
        incomplete_reasons=reasons,
    )

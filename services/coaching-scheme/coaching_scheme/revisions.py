"""The staff-revision timeline: one team-season's coaching regimes, in order.

A revision is a maximal run of consecutive weeks in which the feed names the
same head coach. `effective_from_week` is the run's first week;
`effective_to_week` is the week before the *next* run begins, and is **null for
the last run** — "current", per the spec's field table.

**The end is derived from the next record and never stored**, which is
`venue`'s revision-timeline shape and is the reason two revisions cannot
disagree about where the boundary is. Storing both ends independently lets an
edit to one leave a gap or an overlap that nothing detects.

--------------------------------------------------------------------------
Coach ids are name-derived surrogates, and that is stated on the wire
--------------------------------------------------------------------------

The feed gives names, not ids, and there is no coach-identity service to
resolve them against — `player-identity` is authoritative for players and
deliberately says nothing about staff. So `head_coach_id` is a deterministic
slug of the name: `coach-nick-sirianni`.

Two honest consequences, both of which a consumer must be able to see rather
than discover:

* Two coaches sharing a name would collide onto one id. No such pair exists in
  the feed's history, but nothing here prevents it.
* A spelling change upstream mints a *new* id, which reads as a coaching
  change that did not happen.

The second is the dangerous one, because this collector's whole product is
"when did the regime change". `same_person_as` is not something a slug can
answer, so instead every revision carries `head_coach_name` alongside the id —
a consumer comparing two revisions can see that the id changed while the name
merely re-spelled. `metrics.staff_revisions` is the fleet-visible half: a
season whose revision count jumps without a matching changepoint is either a
re-spelling or a real change the rate series should confirm.
"""

import re
import unicodedata
from dataclasses import dataclass

from .adapters.games import TeamWeekCoach

__all__ = [
    "CHANGE_EVENTS",
    "CHANGE_NONE",
    "CHANGE_UNCLASSIFIED",
    "Revision",
    "build_revisions",
    "coach_id",
]

# The spec's enum, plus one member.
#
# **`unclassified` is a disclosed deviation.** The spec's enum is `none`,
# `dismissal`, `promotion`, `interim`, `play_calling_handoff`. The feed states
# that the head coach changed and says nothing whatever about *why* — a
# dismissal, a resignation, an illness and an interim promotion are
# indistinguishable in a name column. Picking one would be fabrication of
# exactly the kind the spec forbids elsewhere, and `none` is affirmatively
# false. A null was the other candidate and is worse: it loses the fact that a
# change happened at all, which is the one thing the feed *does* establish.
# `broadcast-context` added `weeknight_special` to its own window enum on the
# same reasoning. See the README's deviation list.
CHANGE_NONE = "none"
CHANGE_UNCLASSIFIED = "unclassified"
CHANGE_EVENTS: tuple[str, ...] = (
    CHANGE_NONE,
    "dismissal",
    "promotion",
    "interim",
    "play_calling_handoff",
    CHANGE_UNCLASSIFIED,
)


@dataclass(frozen=True)
class Revision:
    """One staff regime for one team-season."""

    revision_id: str
    team_id: str
    season: int
    effective_from_week: int
    effective_to_week: int | None
    head_coach_id: str | None
    head_coach_name: str | None
    change_event: str
    # The last week this revision is known to run to, null `effective_to_week`
    # or not. Used only for the guard-1 span arithmetic, and NOT published: it
    # is a fact about how far the schedule grid reaches, not about the staff.
    known_last_week: int

    @property
    def last_governed_week(self) -> int:
        """The last week this revision governs, open-ended or not.

        `effective_to_week` is null for the current revision, so a caller
        asking "which weeks does this regime cover" cannot use it directly.
        Not published: it is a fact about how far the schedule grid reaches,
        not about the staff.
        """
        if self.effective_to_week is not None:
            return self.effective_to_week
        return self.known_last_week

    @property
    def week_span(self) -> int:
        """How many weeks this revision governs, inclusive.

        The ceiling `games_sampled` is checked against in guard 1: a team
        plays at most one game a week, so a revision spanning six weeks cannot
        legitimately have sampled seven games.
        """
        return max(0, self.last_governed_week - self.effective_from_week + 1)

    def covers(self, week: int) -> bool:
        if week < self.effective_from_week:
            return False
        if self.effective_to_week is None:
            return True
        return week <= self.effective_to_week


def coach_id(name: str) -> str | None:
    """A stable, name-derived surrogate id. `None` for an empty name.

    Deterministic and offline: the same name always produces the same id, in
    this process and in the next season's. Accents are folded rather than
    dropped so `Kliff Kingsbury` and a diacritic-carrying spelling of the same
    name do not diverge on a byte.
    """
    name = name.strip()
    if not name:
        return None
    folded = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in folded if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return f"coach-{slug}" if slug else None


def build_revisions(
    rows: list[TeamWeekCoach], *, season: int
) -> dict[str, list[Revision]]:
    """`team_id -> [Revision, ...]`, week-ascending, from the schedule feed.

    A week the feed does not carry for a team — a bye — is simply absent from
    the run and does **not** split a revision: the coach on either side is the
    same person, and treating a bye as a boundary would manufacture a regime
    change every team has once a season.

    A blank coach cell yields a revision with a null `head_coach_id` rather
    than being dropped. The team still exists, still plays, and still owes a
    row; what is missing is the name.
    """
    by_team: dict[str, dict[int, str]] = {}
    for row in rows:
        by_team.setdefault(row.team_id, {})[row.week] = row.head_coach_name

    result: dict[str, list[Revision]] = {}
    for team_id, weeks in sorted(by_team.items()):
        ordered = sorted(weeks.items())
        if not ordered:
            continue
        known_last_week = ordered[-1][0]

        # Collapse to runs of the same coach. `starts` holds (week, name) for
        # each run's first week.
        starts: list[tuple[int, str]] = []
        for week, name in ordered:
            if not starts or starts[-1][1] != name:
                starts.append((week, name))

        revisions: list[Revision] = []
        for index, (from_week, name) in enumerate(starts):
            is_last = index == len(starts) - 1
            # Derived from the NEXT run's start, never stored on both sides.
            to_week = None if is_last else starts[index + 1][0] - 1
            revisions.append(
                Revision(
                    revision_id=f"{team_id}-{season}-r{index + 1}",
                    team_id=team_id,
                    season=season,
                    effective_from_week=from_week,
                    effective_to_week=to_week,
                    head_coach_id=coach_id(name),
                    head_coach_name=name.strip() or None,
                    change_event=CHANGE_NONE if index == 0 else CHANGE_UNCLASSIFIED,
                    known_last_week=known_last_week,
                )
            )
        result[team_id] = revisions
    return result

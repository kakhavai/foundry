"""Crew identity and crew churn — who the group behind the name actually is.

The spec's adapter note is the whole of this module: *"an adapter must resolve
individual officials, not just the referee's name, because crews are
reassembled between seasons and substituted within them — attributing rates to
a name is only valid if the group behind that name is stable."*

--------------------------------------------------------------------------
`crew_id` is derived, because no upstream publishes one
--------------------------------------------------------------------------

`officials.csv` has no crew column. NFL crews are identified publicly by their
referee — the white hat — so `crew_id` is `<season>-ref<official_id>`, and the
season is part of it because crews are reassembled between seasons and a
key that outlived one would silently pool two different groups.

**Derived from `official_id`, never from `referee_name`**, and that is not
theoretical tidiness. `games.csv` also carries a `referee` column, and against
2025 it disagrees with `officials.csv` on 17 of 272 games — every one of them
the same man, spelled "Ron Torbert" in one feed and "Ronald Torbert" in the
other. Keyed by name that is a phantom eighteenth crew with 17 games and a
real crew missing them. Keyed by `official_id` it is a footnote, which is what
`referee_disagreement` records it as.

--------------------------------------------------------------------------
`crew_continuity_pct`, and why it is per assignment
--------------------------------------------------------------------------

The spec: *"share of `crew_members` also on this `crew_id` in the sampled
window"*. So it is a property of **one assignment** — this game's seven people
measured against the window the crew's rates were computed over — not a
property of the crew. A crew with one substitute in week 3 has a lower
continuity on that game's assignment and a normal one on the other fifteen,
which is exactly the fact a consumer needs when deciding whether to trust the
rates attached to *this* game.

Each member contributes the share of the window's games they actually worked,
and the assignment's figure is the mean over its members. A member present
throughout contributes 1.0; a one-off substitute in a 16-game window
contributes 0.0625. Against the real 2025 season that puts the median
assignment at 0.955 and drops 5 of 272 below the spec's 0.6 alarm line — a
guard that fires on real data rather than a threshold nothing can reach.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .adapters.officials import CREW_SIZE, REFEREE_POSITION, Official

# The spec's alarm line: below this, the rates describe people who are not
# working the game.
CONTINUITY_ALARM_THRESHOLD = 0.6


@dataclass(frozen=True)
class CrewAssignment:
    """One game's crew, resolved to ids and joined to the modern game id."""

    game_id: str
    legacy_game_id: str
    week: int
    crew_id: str
    referee_id: str
    referee_name: str
    members: tuple[Official, ...]


def crew_id_for(season: int, referee_official_id: str) -> str:
    """The crew key. Season-scoped, and keyed by id rather than by name."""
    return f"{season}-ref{referee_official_id}"


def referee_of(members: Iterable[Official]) -> Official | None:
    """The white hat, or `None` when the feed listed no referee for the game.

    `None` rather than a guess. Without a referee there is no crew key, and
    inventing one from (say) the umpire would produce a crew that exists in no
    other week and whose rates are drawn from a single game.
    """
    for member in members:
        if member.position == REFEREE_POSITION:
            return member
    return None


def build_assignments(
    *,
    season: int,
    games_by_legacy_id: Mapping[str, tuple[str, int]],
    officials_by_legacy_id: Mapping[str, list[Official]],
) -> tuple[list[CrewAssignment], dict[str, str]]:
    """Join the schedule and the officials feed into one assignment per game.

    Returns the assignments and, separately, the **reasons** games in the
    schedule produced none — keyed by modern `game_id`, because that is what
    the caller's coverage accounting is keyed by. Returning the misses rather
    than logging them is what keeps `coverage.expected` honest: every scheduled
    game is owed a row whether or not the officials feed has reached it yet.
    """
    assignments: list[CrewAssignment] = []
    misses: dict[str, str] = {}

    for legacy_id, (game_id, week) in games_by_legacy_id.items():
        members = officials_by_legacy_id.get(legacy_id)
        if not members:
            # The overwhelmingly common miss, and not a defect: this feed is
            # post-game, so every game not yet played lands here.
            misses[game_id] = REASON_CREW_NOT_PUBLISHED
            continue
        referee = referee_of(members)
        if referee is None:
            misses[game_id] = REASON_NO_REFEREE
            continue
        assignments.append(
            CrewAssignment(
                game_id=game_id,
                legacy_game_id=legacy_id,
                week=week,
                crew_id=crew_id_for(season, referee.official_id),
                referee_id=referee.official_id,
                referee_name=referee.name,
                members=tuple(
                    sorted(members, key=lambda m: (m.position, m.official_id))
                ),
            )
        )

    return assignments, misses


REASON_CREW_NOT_PUBLISHED = "crew_not_published"
REASON_NO_REFEREE = "no_referee_in_crew"
REASON_UNDERSIZED_CREW = "undersized_crew"


def undersized(assignment: CrewAssignment) -> bool:
    """Whether the feed listed fewer than a full on-field crew.

    Not an error and not a dropped row — the assignment is still published,
    because who worked the game is a fact even when the feed is incomplete
    about it. It is surfaced as a metric so a feed that quietly starts omitting
    positions is visible before it distorts continuity, which it would: a
    six-person crew's continuity is a mean over six terms.
    """
    return len(assignment.members) < CREW_SIZE


def continuity_pct(
    assignment: CrewAssignment, window: Iterable[CrewAssignment]
) -> float:
    """Share of this assignment's members also on this crew across `window`.

    `window` is the crew's sampled games — the same window the rates are
    computed over, which is what makes the number comparable to them.

    Returns 1.0 for an empty window rather than 0.0 or a division error. An
    empty window means nothing is known about churn, and 0.0 would read as
    *total* churn — it would trip the alarm on every assignment of a crew
    whose rates are not being served anyway, which is the one combination that
    must stay quiet.
    """
    games = list(window)
    if not games or not assignment.members:
        return 1.0

    appearances: dict[str, int] = {}
    for game in games:
        for member in game.members:
            appearances[member.official_id] = appearances.get(member.official_id, 0) + 1

    total = sum(
        appearances.get(member.official_id, 0) / len(games)
        for member in assignment.members
    )
    return total / len(assignment.members)


def referee_disagreement(assignment: CrewAssignment, schedule_referee: str) -> bool:
    """Whether the schedule feed names a different referee than officials.csv.

    A **cross-check**, never a source, and deliberately not a capture failure.
    Both feeds carry display names, and against 2025 the only disagreements
    were "Ron Torbert" vs "Ronald Torbert" — one man, 17 games, zero join
    errors. So this is compared loosely (case, punctuation and the given-name
    form ignored) and what survives is recorded on a counter for a human to
    look at, because a *real* divergence here would mean the crosswalk had
    started joining the wrong games together.
    """
    if not schedule_referee or not assignment.referee_name:
        return False
    return _surname(schedule_referee) != _surname(assignment.referee_name)


def _surname(name: str) -> str:
    """The comparable part of a display name: everything after the first word.

    Given names are where the feeds disagree ("Ron"/"Ronald"); surnames are
    where a genuinely wrong join would show up.
    """
    parts = name.strip().casefold().replace(".", "").split()
    return " ".join(parts[1:]) if len(parts) > 1 else " ".join(parts)

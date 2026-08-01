"""Play-caller identity: a curated register with a mechanical expiry.

This module is the resolution of the sharpest tension in the spec, so the
reasoning is here in full rather than in a commit message.

--------------------------------------------------------------------------
The tension
--------------------------------------------------------------------------

The spec says two things that cannot both be satisfied by any free feed:

> `coverage.expected` means: every team has at least one revision covering the
> requested week, with `play_caller_id` and `play_caller_role` non-null —
> `unknown` is a legitimate role value and counts as present, a null does not.

> Play-caller identity is the field with no reliable feed behind it — it is
> reported in beat coverage, changes without announcement, and is the single
> field most worth a manual override path.

Read literally and honestly together, `staff_assignment` coverage is 0 for
every team, forever.

--------------------------------------------------------------------------
What was rejected, and why
--------------------------------------------------------------------------

**Defaulting `play_caller_id` to the head coach.** The spec itself says the
play-caller is "frequently the head coach, occasionally neither coordinator".
That default would therefore be right most of the time — which is precisely
what makes it poison. A collector that exists to detect a play-calling handoff
would report the handoff week as `play_caller_id` unchanged, because the head
coach did not change. The one signal it was built for is the one the default
erases, and it erases it while looking correct. Not shipped, not
configurable, not available behind a flag.

**Counting `play_caller_role: "unknown"` with a null id as present** (the
"deviate and disclose" option). This reports `ratio: 1.0` on a collector that
knows nothing about its headline field. It is the coverage pathology the
platform rule exists to forbid, wearing a different hat: `expected` would no
longer derive from what succeeded, but `present` would stop meaning anything.
`coverage.ratio` is what a fleet dashboard alerts on, and an always-green
number is worse than an always-red one because nobody investigates it.

**A curated file with no expiry** — the spec's own suggestion, taken
straight. Rejected in that form because a hand-maintained assertion made in
week 1 keeps asserting itself in week 18 with no evidence behind it, and a
play-calling handoff is exactly the event that would falsify it. A stale
override is a defaulted head coach with extra steps.

--------------------------------------------------------------------------
What is shipped
--------------------------------------------------------------------------

The spec's coverage definition, **followed exactly**, over a curated register
in which every entry carries its own provenance and its own expiry:

* `play_caller_id` and `play_caller_role` are populated **only** from an entry
  in `ASSERTIONS` below. Never inferred, never defaulted, never derived from
  the head coach.
* Every entry states a `source` and a week range it is asserted over. Past
  `asserted_through_week` the entry stops applying and the team goes back to
  missing, with the reason `play_caller_assertion_expired` — which is the
  staleness story, mechanical rather than aspirational. It expires whether or
  not anyone remembers to revisit it.
* The register **ships empty**, because that is the honest state of this
  repository's knowledge. `staff_assignment` therefore reports
  `expected: 32, present: 0` and names all 32 teams in `coverage.missing`,
  with one priority error saying where the fix goes.

Two things keep that from being a dead red light. First, it is *movable*: the
remedy is a line in this file, named in the envelope, rather than an upstream
nobody controls. Second, it is *isolated*: `team_scheme_profile` has its own
coverage over a fully sourceable universe and reaches 1.0 on a healthy pass,
so the collector's other half is not dragged to zero with it. An operator sees
a curation gap next to a working rate pipeline, which is what this actually is.

--------------------------------------------------------------------------
Validation is at import, and it is fatal
--------------------------------------------------------------------------

A malformed entry raises on import, which fails the pod's startup. That is the
right blast radius: this is the one file in the collector whose content is
hand-typed, so a typo here is a *curation* defect and it must not be able to
degrade quietly into a wrong attribution on 1/32 of the league.
"""

from dataclasses import dataclass

__all__ = [
    "ASSERTIONS",
    "REASON_SPLIT_WITHIN_REVISION",
    "PLAY_CALLER_ROLES",
    "REASON_EXPIRED",
    "REASON_NOT_YET_EFFECTIVE",
    "REASON_UNKNOWN",
    "PlayCallerAssertion",
    "resolve",
    "resolve_for_span",
    "validate",
]

# The spec's enum, unchanged. `unknown` is a legitimate role: it says "somebody
# specific calls the plays and we have identified them, but not their title" —
# which is a real state a beat report can leave you in, and is why the spec
# calls it present.
PLAY_CALLER_ROLES: frozenset[str] = frozenset(
    {"head_coach", "offensive_coordinator", "position_coach", "unknown"}
)

REASON_UNKNOWN = "play_caller_unknown"
REASON_EXPIRED = "play_caller_assertion_expired"
REASON_NOT_YET_EFFECTIVE = "play_caller_assertion_not_yet_effective"
# The register says the play-caller changed *inside* one staff revision — a
# play-calling handoff with no staff change, which is a real event and the one
# `change_event: play_calling_handoff` names. One row cannot state two
# play-callers, and picking either would attach a fact about one regime to
# another. Refused, with the ambiguity named.
REASON_SPLIT_WITHIN_REVISION = "play_caller_changed_within_revision"


@dataclass(frozen=True)
class PlayCallerAssertion:
    """One curated claim about who calls a team's offensive plays.

    `asserted_through_week` is not a guess about the future — it is a
    statement of how far the evidence in `source` actually reaches. A beat
    report published in week 3 says nothing about week 12, so an entry sourced
    from it ends at the week its author could see.
    """

    team_id: str
    season: int
    play_caller_id: str
    play_caller_role: str
    effective_from_week: int
    asserted_through_week: int
    source: str


# EMPTY ON PURPOSE. See the module docstring: an empty register is the honest
# state of this repository's knowledge, and `staff_assignment` coverage reports
# it as 0/32 rather than papering over it.
#
# To add one, state the evidence in `source` — a URL, a dated report, a press
# conference — and set `asserted_through_week` to the last week that evidence
# actually covers. Do not extend it to the end of the season because it is
# tidier; the expiry is the only thing standing between this file and a stale
# claim that outlives its own justification.
ASSERTIONS: tuple[PlayCallerAssertion, ...] = ()


def validate(assertions: tuple[PlayCallerAssertion, ...] | None = None) -> None:
    """Reject a malformed or self-contradicting register. Raises `ValueError`.

    Called at import below, so a curation defect fails the process rather than
    mis-attributing one team's play-caller for a season.

    `None` means "the module's own register", read at CALL time. A default of
    `ASSERTIONS` would bind the tuple at definition time — correct in
    production, where it never changes, and quietly wrong anywhere the
    register is substituted, because the substitution would have no effect and
    the test would still pass against the shipped empty tuple.
    """
    assertions = ASSERTIONS if assertions is None else assertions
    seen: dict[tuple[str, int], list[PlayCallerAssertion]] = {}
    for entry in assertions:
        if entry.play_caller_role not in PLAY_CALLER_ROLES:
            raise ValueError(
                f"{entry.team_id} {entry.season}: role "
                f"{entry.play_caller_role!r} is not one of "
                f"{sorted(PLAY_CALLER_ROLES)}"
            )
        if not entry.play_caller_id.strip():
            raise ValueError(f"{entry.team_id} {entry.season}: empty play_caller_id")
        if not entry.source.strip():
            # An unsourced assertion is indistinguishable from a guess, and
            # this whole module exists to keep guesses out.
            raise ValueError(
                f"{entry.team_id} {entry.season}: every assertion must cite a source"
            )
        if entry.asserted_through_week < entry.effective_from_week:
            raise ValueError(
                f"{entry.team_id} {entry.season}: asserted_through_week "
                f"{entry.asserted_through_week} precedes effective_from_week "
                f"{entry.effective_from_week}"
            )
        seen.setdefault((entry.team_id, entry.season), []).append(entry)

    for (team_id, season), entries in seen.items():
        ordered = sorted(entries, key=lambda e: e.effective_from_week)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if later.effective_from_week <= earlier.asserted_through_week:
                raise ValueError(
                    f"{team_id} {season}: assertions overlap at week "
                    f"{later.effective_from_week}"
                )


def resolve(
    team_id: str,
    season: int,
    week: int,
    *,
    assertions: tuple[PlayCallerAssertion, ...] | None = None,
) -> tuple[PlayCallerAssertion | None, str | None]:
    """The assertion governing one team-week, or `(None, reason)`.

    Three distinct refusals, and they are distinct because they imply
    different work. `play_caller_unknown` means nobody has ever curated this
    team; `play_caller_assertion_expired` means somebody did and the evidence
    has run out — a re-check, not a first look. Collapsing them into one
    reason would hide the difference between an empty register and a neglected
    one, which is precisely the difference an operator needs.

    `None` reads the module's register at CALL time — see `validate` for why
    a `= ASSERTIONS` default would be a trap.
    """
    assertions = ASSERTIONS if assertions is None else assertions
    candidates = [
        entry
        for entry in assertions
        if entry.team_id == team_id and entry.season == season
    ]
    if not candidates:
        return None, REASON_UNKNOWN
    for entry in candidates:
        if entry.effective_from_week <= week <= entry.asserted_through_week:
            return entry, None
    if all(week < entry.effective_from_week for entry in candidates):
        return None, REASON_NOT_YET_EFFECTIVE
    return None, REASON_EXPIRED


def resolve_for_span(
    team_id: str,
    season: int,
    from_week: int,
    to_week: int,
    *,
    assertions: tuple[PlayCallerAssertion, ...] | None = None,
) -> tuple[PlayCallerAssertion | None, str | None]:
    """The one assertion governing an ENTIRE revision span, or `(None, reason)`.

    **This exists because resolving at the query week is a real defect, not a
    simplification.** A register asserting a play-caller for weeks 9-12,
    queried at week 10, would otherwise stamp that person — and their cited
    source — onto the weeks 1-8 regime as well. That is the spec's own named
    failure mode (a fact about one regime attached to another) applied to staff
    identity instead of rates, and it is exactly what the expiry mechanism
    exists to prevent, bypassed by evaluating the expiry at the wrong week.

    Requiring the assertion to cover the **whole** span is what preserves the
    expiry's guarantee in the other direction too. Resolving at the revision's
    first week alone would let an assertion sourced through week 12 be stamped
    on a revision running to week 17 — the staleness bug, moved rather than
    fixed.

    A revision whose weeks resolve to two different assertions is refused with
    `REASON_SPLIT_WITHIN_REVISION` rather than given either: one row cannot
    state two play-callers, and choosing one would be the same regime-mixing
    error again.
    """
    governing: PlayCallerAssertion | None = None
    for week in range(from_week, to_week + 1):
        entry, reason = resolve(team_id, season, week, assertions=assertions)
        if entry is None:
            return None, reason
        if governing is None:
            governing = entry
        elif entry is not governing:
            return None, REASON_SPLIT_WITHIN_REVISION
    if governing is None:
        # An empty span. Not reachable from a well-formed revision, and a
        # silent `None, None` would read as "resolved to nothing", which is
        # not a state any caller should have to handle.
        return None, REASON_UNKNOWN
    return governing, None


validate()

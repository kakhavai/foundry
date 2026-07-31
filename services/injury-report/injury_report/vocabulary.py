"""The normalized vocabularies `injury-report` publishes, and nothing else.

These are the *contract* side, not the wire side: every enum here appears in
`contracts/signal-envelope/collectors/injury-report.json` and in the phase
doc's field table. `adapters/upstream.py` owns the mapping from whatever an
upstream writes onto these; keeping the target set in its own module is what
lets `report.py` reason about a designation without importing the wire.

**An unrecognised value is loud, never passed through.** The Phase 8 spec's
failure-handling section is explicit that an upstream renaming (or inventing) a
value must fail rather than land in an append-only lake nobody rewrites, and an
injury designation is a small controlled vocabulary — "Probable" was retired
from the league's report in 2016, so a feed still emitting it is describing a
different thing than this collector's `questionable`. `UnknownVocabulary`
carries the field and the raw value so the caller can record both.

**Body part is the deliberate exception.** The spec says the enum must
"degrade to `unspecified` rather than guess", because upstream text runs from
`knee` through `left knee (patellar tendinitis)` to `lower body`. Guessing a
body part mislabels a row; declining to name one loses only specificity, and
`body_part_raw` keeps the original either way.
"""

from types import MappingProxyType

# `null` is a fourth state and is deliberately NOT in this tuple: it means "no
# designation has been published yet", which is a different fact from `none`
# ("published, and this player carries no designation"). Distinguishing those
# two is the reason this collector exists — see the phase doc.
GAME_DESIGNATIONS: tuple[str, ...] = ("out", "doubtful", "questionable", "none")

PARTICIPATIONS: tuple[str, ...] = ("dnp", "limited", "full")

ABSENCE_REASONS: tuple[str, ...] = (
    "injury",
    "rest",
    "illness",
    "personal",
    "suspension",
    "unspecified",
)

# The reasons that make `is_non_injury` true. `unspecified` is deliberately not
# among them: "we do not know why" is not "we know it was not an injury", and
# the spec's failure mode here is a scheduled rest day coded identically to a
# player who could not practise.
NON_INJURY_REASONS: frozenset[str] = frozenset(
    {"rest", "illness", "personal", "suspension"}
)

ROSTER_STATUSES: tuple[str, ...] = (
    "active",
    "ir",
    "ir_designated_return",
    "pup",
    "nfi",
    "suspended",
)

SIDES_OF_BALL: tuple[str, ...] = ("offense", "defense", "special_teams")

# In publication order, which is also the order the league's practice reports
# are filed in. `report.py` relies on that ordering when it appends to
# `practice_participation` and `designation_history`.
PRACTICE_DAYS: tuple[str, ...] = ("wednesday", "thursday", "friday")

BODY_PARTS: tuple[str, ...] = (
    "abdomen",
    "achilles",
    "ankle",
    "back",
    "biceps",
    "calf",
    "chest",
    "concussion",
    "elbow",
    "finger",
    "foot",
    "forearm",
    "groin",
    "hamstring",
    "hand",
    "hip",
    "illness",
    "knee",
    "neck",
    "pectoral",
    "quadriceps",
    "ribs",
    "shoulder",
    "thigh",
    "thumb",
    "toe",
    "triceps",
    "wrist",
    "unspecified",
)

# Position code -> side of the ball. The spec wants `side_of_ball` so the
# generator can find *opponent-side* entries, which is the whole reason this
# collector ignores the roster scope; deriving it from `position` rather than
# requiring an upstream column keeps that working against feeds that only
# publish a position.
_POSITION_SIDES: dict[str, str] = {
    **dict.fromkeys(
        ("QB", "RB", "FB", "WR", "TE", "OL", "OT", "OG", "C", "G", "T"), "offense"
    ),
    **dict.fromkeys(
        (
            "DL",
            "DE",
            "DT",
            "NT",
            "LB",
            "ILB",
            "OLB",
            "MLB",
            "EDGE",
            "CB",
            "S",
            "FS",
            "SS",
            "DB",
        ),
        "defense",
    ),
    **dict.fromkeys(("K", "P", "LS", "KR", "PR"), "special_teams"),
}
POSITION_SIDES = MappingProxyType(_POSITION_SIDES)


class UnknownVocabulary(ValueError):
    """An upstream value that does not map onto a published vocabulary.

    Subclasses `ValueError`, so `CollectorMetrics.reason_for` classifies it as
    `malformed` if it ever escapes to the pass level. It normally does not: a
    single unmappable row is dropped and recorded, because one feed oddity is
    not a reason to lose the other thirty-one teams' reports.
    """

    def __init__(self, field: str, raw: str) -> None:
        super().__init__(f"unknown {field}: {raw!r}")
        self.field = field
        self.raw = raw
        self.reason = f"unknown_{field}"


def side_of_ball(position: str) -> str:
    """Map a position code onto a side of the ball, or refuse.

    Refusing rather than defaulting to `offense`: a defender silently filed as
    offence is invisible to the exact query — "who is out on the other side of
    this matchup" — that this collector exists to answer.
    """
    code = (position or "").strip().upper()
    side = POSITION_SIDES.get(code)
    if side is None:
        raise UnknownVocabulary("position", position)
    return side


def body_part(raw: str) -> str:
    """Normalize free upstream text onto `BODY_PARTS`, degrading to
    `unspecified` rather than guessing.

    Substring matching, longest name first, so `hamstring` is not shadowed by a
    shorter member and `left knee (patellar tendinitis)` still reads `knee`.
    Text naming two parts resolves to the first match in that order, which is
    arbitrary but stable; `body_part_raw` is what a consumer reads when the
    specificity matters.
    """
    text = (raw or "").strip().lower()
    if not text:
        return "unspecified"
    for name in sorted(BODY_PARTS, key=len, reverse=True):
        if name != "unspecified" and name in text:
            return name
    return "unspecified"

"""Resolution: the matchup scope -- the opposing players (and our own line)
that determine how our SCOPED players perform.

Mirrors `scope.py`'s slot-resolution loop, but strips out everything that
loop needs only for *membership*: there is no version ledger, no
carry-forward, no manual overrides, and no `team_defense` special case,
because `MATCHUP_RULES` has none of those -- every matchup rule is
`entity_type="player"`. What survives unchanged is the property the loop
exists to protect: `coverage.expected` is seeded from the config's own slot
keys *before* a single row is looked at, so a resolution failure -- an
unrecognised team, an unrecognised position, a rank the chart never reached,
an unresolvable name -- shrinks `present`, never `expected`. A truncated
upstream must read as a low ratio, not a perfect one.

`OL` in `MATCHUP_RULES` is OUR OWN line, not the opponent's -- pass
protection bears on the QB and RB already in the player scope. Every other
rule here (`CB`, `S`, `LB`, `DL`) is an opposing player. This module does not
know or care which side of the ball a row came from; that distinction lives
entirely in `rules.MATCHUP_RULES`'s own comments.

This envelope carries its **own** `CoverageAccumulator`, never `scope.py`'s.
A matchup resolution failure must not mask a healthy player scope, and vice
versa -- sharing one accumulator between the two envelopes would blend two
independent facts into one misleading number.
"""

from datetime import datetime

from collector_core.coverage import CoverageAccumulator

from .adapters.identity import PlayerIdentityResolver, PlayerRef, UnresolvablePlayer
from .rules import MATCHUP_RULES, TEAMS, canonical_position, canonical_team, slot_key

MATCHUP_SIGNAL = "scope_matchup_weekly"

_RULES_BY_POSITION = {rule.position: rule for rule in MATCHUP_RULES}


def expected_matchup_keys() -> tuple[str, ...]:
    """Every matchup slot the config demands, derived from `MATCHUP_RULES`
    alone -- the matchup analogue of `rules.expected_slots()`.

    Built before any row is looked at, for the same reason `expected_slots()`
    is: an expectation built from what the upstream returned reports a
    truncated document as ratio 1.0. Its length always agrees with
    `rules.expected_matchup_slots()` (608 = 32 teams x 19 slots); that
    function gives the count alone; this one gives the actual keys a
    `CoverageAccumulator` needs to seed `missing` correctly on a total
    outage.
    """
    return tuple(
        slot_key(team, rule.rule_id, rank)
        for team in TEAMS
        for rule in MATCHUP_RULES
        for rank in range(1, rule.max_depth + 1)
    )


async def resolve_matchup_slots(
    rows: list[dict],
    *,
    season: int,
    week: int,
    now: datetime,
    resolver: PlayerIdentityResolver,
) -> tuple[list[dict], CoverageAccumulator]:
    """Fill every matchup slot the config demands from a flat list of chart
    rows.

    `rows` is the flattened depth chart passed in by `capture.py` -- one
    dict per row, carrying `team`/`position`/`depth_rank`/`name` exactly as
    the upstream chart printed them, uncanonicalized. Canonicalizing here
    (rather than upstream) keeps the drop-never-guess rule in one place: a
    row whose `canonical_team` or `canonical_position` returns `None` is
    dropped, and a `depth_rank` beyond its rule's `max_depth` is dropped too
    -- both leave their slot reading as missing rather than inventing one
    for them.

    `season`/`week` describe the envelope this feeds, not any field on a
    row -- mirrors `scope_membership_weekly`, whose rows carry no season or
    week either, because that belongs to the envelope's own `scope` block.
    `now` is accepted for the same reason `capture.py`'s other resolvers
    take a frozen instant, even though today's row shape has no per-row
    timestamp to stamp with it.
    """
    acc = CoverageAccumulator(expected_matchup_keys())
    signals: list[dict] = []

    for row in rows:
        team = canonical_team(row.get("team"))
        if team is None:
            continue
        position = canonical_position(row.get("position"))
        if position is None:
            continue
        rule = _RULES_BY_POSITION.get(position)
        if rule is None:
            # A real, canonical position -- just not one the matchup scope
            # asks about (a QB or WR row surviving the depth-chart adapter's
            # WANTED_POSITIONS filter for the *player* scope's sake).
            continue
        rank = row.get("depth_rank")
        if not isinstance(rank, int) or not (1 <= rank <= rule.max_depth):
            # Not just "beyond the quota": a missing or non-integer rank is
            # exactly as unusable as an excess one, and both must leave the
            # slot missing rather than guess a rank for it.
            continue

        key = slot_key(team, rule.rule_id, rank)
        ref = PlayerRef(row.get("name", ""), team, position)
        try:
            player_id = await resolver.resolve(ref)
        except UnresolvablePlayer as exc:
            # Never a skipped row: the slot was real (a valid team, rule and
            # rank), so the failure is recorded against it and it reads as
            # missing rather than silently absent.
            acc.fail(key, exc.reason)
            continue

        acc.record(key)
        signals.append(
            {
                "player_id": player_id,
                "slot_key": key,
                "rule_id": rule.rule_id,
                "team": team,
                "position": position,
                "depth_rank": rank,
            }
        )

    return signals, acc

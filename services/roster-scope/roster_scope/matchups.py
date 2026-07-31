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

**Co-listed names reuse `scope.py`'s `split_co_listed`, not a second
implementation of it.** `"Corner A OR Corner B"` is two real players, and
resolving the raw, unsplit string would mint one fabricated identity that
matches neither -- an append-only-lake mistake, not a recoverable one. But
this module's rank model is not `scope.py`'s: `resolve_membership` discards
the upstream's own rank entirely and recomputes it from a candidate's
position in a per-position, co-listing-expanded, re-sorted list (so a
co-listing at chart-rank 1 consumes ranks 1 *and* 2 of the resolved output,
shifting everyone below down by one). This module is row-driven -- a row's
own reported `depth_rank` is the slot it fills, full stop -- and
reimplementing that renumbering here would mean no longer trusting the
upstream's own rank for every *other* row too, a much bigger behavioural
change than fixing the fabrication bug calls for. So a co-listed row takes
the **ordered-first** split name (the same normalized-name tie-break
`scope.py` uses when the chart declines to order two players) for its own
declared rank, and the second name is dropped for this pass -- not
reassigned to the next rank, and not marked missing under its own key,
because this module has no key for "the second name in a co-listing that
already has one."

**The drop is recorded, not silent.** A real depth chart lists contiguous
ranks, so the rank right after a co-listing is almost always claimed by a
*distinct* row -- the dropped name then has zero natural trace: not
present, not missing (its slot is filled by someone else), and, without
the `acc.add_error(...)` call below, not in `errors` either. `coverage`
would read as fully healthy while a real player who was genuinely on the
chart silently never appears anywhere in this envelope -- the same shape
as a truncated upstream reporting ratio 1.0, just relocated from a slot to
a name. So every dropped co-listed name is recorded against the
accumulator with reason `co_listed_counterpart_dropped`, capped the same
way every other error is (`CoverageAccumulator.errors` runs `cap_errors`
regardless of how it was populated), so a chart full of co-listings still
produces a bounded errors array rather than one entry per row.
"""

from datetime import UTC, datetime

from collector_core.coverage import CoverageAccumulator

from .adapters.identity import PlayerIdentityResolver, PlayerRef, UnresolvablePlayer
from .rules import (
    MATCHUP_RULES,
    canonical_position,
    canonical_team,
    expected_matchup_keys,
    slot_key,
)
from .scope import split_co_listed

MATCHUP_SIGNAL = "scope_matchup_weekly"

_RULES_BY_POSITION = {rule.position: rule for rule in MATCHUP_RULES}


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


async def resolve_matchup_slots(
    rows: list[dict],
    *,
    season: int,
    week: int,
    now: datetime,
    resolver: PlayerIdentityResolver,
    deadline: datetime | None = None,
    clock=_utc_now,
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

    `deadline`/`clock` mirror `resolve_membership`'s: today's stub identity
    resolver is instant, so this is inert, but once the HTTP resolver is
    wired in, up to 608 sequential awaits need the same wall-clock escape
    valve membership's 416 already have. Checked once per row (the natural
    unit here, where membership checks once per team) rather than wrapping
    the pass in a timeout, for the same reason: cancelling the coroutine
    would discard everything already resolved, where this preserves the
    partial capture and accounts for the rest as `deadline_exceeded`.
    """
    acc = CoverageAccumulator(expected_matchup_keys())
    signals: list[dict] = []
    truncated = False

    for row in rows:
        if deadline is not None and not truncated and clock() >= deadline:
            truncated = True

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

        if truncated:
            acc.fail(key, "deadline_exceeded")
            continue

        # `split_co_listed` always returns at least one name (or an empty
        # list for a blank raw string) -- see the module docstring for why
        # only the ordered-first candidate is taken here rather than
        # reassigning the rest to later ranks the way `scope.py` does.
        names = split_co_listed(row.get("name", ""))
        name = names[0] if names else ""

        for dropped in names[1:]:
            # Recorded even though the row is about to be resolved and
            # filled normally -- the drop is real regardless of whether the
            # ordered-first name succeeds, and it must not depend on this
            # slot's own rank happening to come up empty to be visible.
            acc.add_error(
                "co_listed_counterpart_dropped",
                f"{key}: {dropped!r} co-listed with {name!r}, not resolved",
            )

        ref = PlayerRef(name, team, position)
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

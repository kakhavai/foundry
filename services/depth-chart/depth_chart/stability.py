"""Whether the ordering can be believed — the second half of this collector.

The spec's framing: *"The published depth chart and the functional one disagree
often enough that treating them as the same field would be an error... Carrying
both, plus how long the current ordering has held, lets the generator weight the
chart by its own reliability instead of trusting it flatly."*

Everything here is a pure function over `adapters.upstream.PositionHistory`, so
it is testable without a network, a lake, or a clock beyond the one passed in.

**The failure mode this module exists to make visible.** A frozen upstream is
the dangerous case, and it is dangerous precisely because nothing in the row
looks wrong: if a vendor keeps serving a preseason chart, `weeks_at_rank` climbs
every week and `stability_score` converges on 1.0 — the collector reports its
strongest possible signal exactly when the data is dead. `stability_score`
cannot detect that on its own, because a genuinely settled ordering produces the
identical number. `chart_age_days` and `is_stale` are the independent axis, and
they are what a consumer cross-checks against a high `stability_score`.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .adapters.upstream import PositionHistory, WeekOrdering

# The trailing window `rank_changes_4w` counts over, in ISO weeks. Four weeks
# means at most three week-to-week comparisons.
STABILITY_WINDOW_WEEKS = 4

# A chart older than this during the season is treated as stale. Straight from
# the spec's failure-handling note: "alert when any team's chart is older than
# 10 days during the season".
STALE_AFTER_DAYS = 10


def chart_hash(ordering: WeekOrdering) -> str:
    """Content hash of one `(team, position)` ordering.

    Computed over a **canonically sorted** `(rank, player)` sequence rather than
    over the response bytes, which the spec calls out as the hard part: several
    upstreams re-serialize an unchanged chart with a different tie-break order,
    and hashing the bytes would report a promotion that never happened.

    `gsis_id` is preferred over the name where present, so a vendor correcting
    a player's spelling does not read as a roster move.
    """
    canonical = "|".join(
        f"{entry.rank}:{entry.gsis_id or entry.player_name}"
        for entry in sorted(ordering.entries, key=lambda e: e.rank)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _rank_of(ordering: WeekOrdering, gsis_id: str | None, name: str) -> int | None:
    for entry in ordering.entries:
        if gsis_id is not None and entry.gsis_id is not None:
            if entry.gsis_id == gsis_id:
                return entry.rank
        elif entry.player_name == name:
            return entry.rank
    return None


def weeks_at_rank(history: PositionHistory, gsis_id: str | None, name: str) -> int:
    """Consecutive publication weeks this player has held their current rank.

    Counted backwards from the newest retained week and stopped at the first
    week where the rank differs or the player is absent. **It is a lower bound**,
    bounded by `adapters.upstream.MAX_WEEKS` of retained history: a player who
    has genuinely held WR1 all season reports the retention ceiling, not the
    true count. Reporting the bound honestly is preferred to reading further
    back, which would mean retaining the whole season per group.
    """
    current = _rank_of(history.current, gsis_id, name)
    if current is None:
        return 0
    weeks = 1
    for ordering in history.weeks[1:]:
        if _rank_of(ordering, gsis_id, name) != current:
            break
        weeks += 1
    return weeks


def rank_changes(history: PositionHistory, window: int = STABILITY_WINDOW_WEEKS) -> int:
    """How many times this group's ordering changed over the trailing window.

    A week-to-week comparison of `chart_hash`, so a re-publish of an unchanged
    chart counts as zero changes and a promotion counts as one.
    """
    considered = history.weeks[:window]
    hashes = [chart_hash(ordering) for ordering in considered]
    return sum(1 for a, b in zip(hashes, hashes[1:]) if a != b)


def comparisons(history: PositionHistory, window: int = STABILITY_WINDOW_WEEKS) -> int:
    """How many week-to-week comparisons the retained history supports.

    The denominator `stability_score` divides by. Exposed rather than inlined
    because "no changes over three comparisons" and "no changes because there
    was only one week to compare" are completely different claims, and a score
    that collapsed them would report a first-ever capture as maximally stable.
    """
    return max(0, len(history.weeks[:window]) - 1)


def stability_score(history: PositionHistory) -> float | None:
    """0-1, higher meaning the ordering has held. `None` when undeterminable.

    `1 - changes/comparisons`. **`None`, not 1.0, when there is nothing to
    compare** — a group seen in exactly one publication week has produced no
    evidence of stability, and `0/0` reading as a perfect score is the same
    arithmetic trap `Coverage.ratio` guards against with its floor.
    """
    total = comparisons(history)
    if total == 0:
        return None
    return round(1.0 - rank_changes(history) / total, 4)


@dataclass(frozen=True)
class Freshness:
    """How old the chart backing a group is, measured against `now`."""

    chart_published_at: datetime
    age_days: float
    is_stale: bool


def freshness(history: PositionHistory, now: datetime) -> Freshness:
    """Age of the current ordering's publication timestamp.

    Measured from `chart_published_at` (the upstream's own `dt`) and never from
    the fetch instant. A vendor re-serving a frozen chart refreshes the fetch
    every 15 minutes while `dt` stands still, so anchoring on the fetch would
    report a dead chart as perpetually fresh.
    """
    published = history.current.captured_at
    age = (now - published).total_seconds() / 86400.0
    return Freshness(
        chart_published_at=published,
        age_days=round(age, 3),
        is_stale=age > STALE_AFTER_DAYS,
    )

"""The spec's named failure mode, as a publish-time cross-field assertion.

> When a starter goes to IR mid-week, the unit's aggregate grades do not move,
> because they are computed over snaps the departed player took — the line
> keeps its elite `pressure_rate_allowed_adj` into the exact week it will be
> worst, and the sack-risk projection for that quarterback is confidently
> wrong in the wrong direction. **Nothing about the row looks malformed.**

That last sentence is why this is code rather than a comment. Every field is
populated, every value is in range, coverage is 1.0, the schema validates and
the differential against `defensive-front` computes cleanly. There is no
observable downstream of a stale unit row except the projection being wrong.

--------------------------------------------------------------------------
It **fails** rather than flags, and that is deliberate
--------------------------------------------------------------------------

`defensive-front`'s timing guard flags: a residual slope is a statistical
verdict about a whole league-week, it has a false-positive rate, and a pass
that fires it still carries thirty-two rows a consumer may legitimately want.
This one is not that. It asserts an **arithmetic invariant over rows this
process just built**: if `lineup_changed` is true then the correction was
applied, and `pressure_rate_allowed_adj` is `pressure_rate_allowed_adj_observed
+ lineup_adjustment_pressure_rate`. There is no data under which a correctly
implemented `build_rows` violates that. A violation is therefore a **defect in
this collector**, not a fact about the season, and shipping a flagged row would
publish a number that is simply wrong under a name a generator trusts.

So the raise ends the pass: `capture` routes it through
`collector_core.failure.fail_capture`, which writes one `present: 0` envelope
with the reason and re-raises, leaving the last good capture serving on
`/signals`. One pass of freshness against a season of a silently wrong
sack-risk input.

--------------------------------------------------------------------------
What is NOT a guard failure
--------------------------------------------------------------------------

"The lineup changed and this pass has no replacement delta to correct it with"
is **missing input data, not a defect**, and it is handled one layer up:
`ratings.build_rows` drops that team into `coverage.missing` with a reason and
emits no row for it at all. Both paths honour the spec's requirement that a
stale unit must not publish; only one of them is a crash. Failing the entire
league's capture because one team's backup has no observed sample would make a
data gap look like a code fault, and would take out thirty-one correct rows
with it.
"""

from collections.abc import Sequence

from .ratings import PRECISION, RECORD_UNIT

__all__ = ["STALE_UNIT_REASON", "StaleUnitRow", "assert_lineup_guard"]

STALE_UNIT_REASON = "stale_unit_after_lineup_change"

# The published rates are rounded to `PRECISION`, so the identity
# `adj == observed + delta` is checked at the same resolution the row carries.
# A tighter tolerance would fail on rounding; a looser one would accept a
# correction that was applied to the wrong metric.
_TOLERANCE = 10.0**-PRECISION / 2.0


class StaleUnitRow(RuntimeError):
    """A unit row whose lineup changed but whose adjusted rate did not."""


def assert_lineup_guard(rows: Sequence[dict]) -> None:
    """Raise `StaleUnitRow` for the first row that violates the invariant.

    Run over **exactly what is about to be published**, after `build_rows` and
    before the envelope is built — the same placement `defensive-front` uses
    for its own guard, and for the same reason: a guard that inspects an
    intermediate structure proves nothing about the rows that reach the lake.

    Three arms, and each one is separately reachable by a mutation:

    1. `continuity_games == 0`. The spec states it outright. Structurally
       guaranteed by `continuity_games`' walk-back today, which makes this a
       defence of an invariant rather than of a live path — and inlining it
       would make the defence unprovable.
    2. A correction was actually computed, and it is **non-zero**. A changed
       lineup that corrects by exactly nothing is indistinguishable from one
       that was never corrected, which is the stale row this exists to catch.
    3. The published `pressure_rate_allowed_adj` equals
       `pressure_rate_allowed_adj_observed + lineup_adjustment_pressure_rate`.
       This is the arm that catches the real defect: a correction that was
       computed, published in its own column, and never applied to the number
       a generator reads.
    """
    for row in rows:
        if row.get("record_type") != RECORD_UNIT or not row.get("lineup_changed"):
            continue
        team = row.get("team_id")

        if row.get("continuity_games") != 0:
            raise StaleUnitRow(
                f"{team}: lineup_hash changed but continuity_games is "
                f"{row.get('continuity_games')!r}, not 0"
            )

        delta = row.get("lineup_adjustment_pressure_rate")
        if delta is None or delta == 0.0:
            raise StaleUnitRow(
                f"{team}: lineup_hash changed but no replacement correction was "
                f"applied (lineup_adjustment_pressure_rate={delta!r})"
            )

        observed = row.get("pressure_rate_allowed_adj_observed")
        published = row.get("pressure_rate_allowed_adj")
        if observed is None or published is None:
            raise StaleUnitRow(
                f"{team}: lineup_hash changed but the adjusted pressure rate is "
                f"null, so the correction cannot have been applied"
            )
        if abs(published - (observed + delta)) > _TOLERANCE:
            raise StaleUnitRow(
                f"{team}: lineup_hash changed and the unit's adjusted metrics "
                f"were not recomputed — pressure_rate_allowed_adj is "
                f"{published!r}, but observed {observed!r} plus the "
                f"replacement correction {delta!r} is {observed + delta!r}"
            )

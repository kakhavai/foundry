"""Matchup-scope resolution: coverage seeded from config, never from the
fetch, and the drop-never-guess rule for team/position/rank."""

from datetime import UTC, datetime, timedelta

import pytest

from roster_scope.matchups import resolve_matchup_slots
from roster_scope.rules import expected_matchup_slots

NOW = datetime(2026, 9, 2, tzinfo=UTC)


class StubResolver:
    async def resolve(self, ref):
        return f"fdy-{ref.name.lower().replace(' ', '-')}"


def _row(team, position, rank, name):
    return {"team": team, "position": position, "depth_rank": rank, "name": name}


async def test_expected_is_the_config_total_not_what_the_upstream_returned():
    """A truncated chart must not shrink the denominator -- that is the
    ratio-1.0 bug."""
    rows = [_row("KC", "CB", 1, "A Corner")]
    _, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    envelope_coverage = acc.result()
    assert envelope_coverage.expected == expected_matchup_slots() == 608
    assert envelope_coverage.present == 1


async def test_a_total_outage_reports_a_low_ratio_not_a_perfect_one():
    _, acc = await resolve_matchup_slots(
        [], season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    coverage = acc.result()
    assert coverage.present == 0
    assert coverage.ratio < 0.01, coverage.ratio
    assert len(coverage.missing) > 0


async def test_rows_beyond_the_quota_are_dropped():
    rows = [_row("KC", "CB", rank, f"Corner {rank}") for rank in range(1, 7)]
    signals, _ = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert len(signals) == 4, [s["slot_key"] for s in signals]


async def test_an_unknown_position_is_counted_missing_not_guessed():
    rows = [_row("KC", "PUNTER", 1, "A Punter")]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert signals == []
    assert len(signals) == 0
    assert acc.result().present == 0


async def test_an_unknown_team_is_counted_missing_not_guessed():
    """The team-side twin of the position check above -- both `canonical_*`
    lookups are drop-never-guess, and only one of the two was exercised by
    the brief's test set."""
    rows = [_row("ZZZ", "CB", 1, "A Corner")]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert signals == []
    assert acc.result().present == 0


async def test_a_recognised_but_non_matchup_position_is_dropped():
    """`WR` is a real, canonical position -- just not one `MATCHUP_RULES`
    asks about. It must not be admitted just because `canonical_position`
    recognises it."""
    rows = [_row("KC", "WR", 1, "A Receiver")]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert signals == []
    assert acc.result().present == 0


async def test_an_unresolvable_name_is_missing_not_skipped():
    """A resolver refusal must still count against the slot's key -- never a
    silently skipped row, which would shrink numerator and denominator
    together and read as perfect coverage."""

    class RefusingResolver:
        async def resolve(self, ref):
            from roster_scope.adapters.identity import UnresolvablePlayer

            raise UnresolvablePlayer("identity_unresolvable_name", ref.name)

    rows = [_row("KC", "CB", 1, "A Corner")]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=RefusingResolver()
    )
    assert signals == []
    coverage = acc.result()
    assert coverage.present == 0
    assert "KC:cb_matchup_le_4:1" in coverage.missing
    assert any(e["reason"] == "identity_unresolvable_name" for e in acc.errors)


async def test_every_matchup_rule_can_fill_its_own_slot():
    """One row per rule, at rank 1 -- proves the position-to-rule lookup
    covers all five matchup positions, not just `CB`."""
    positions = ["CB", "S", "LB", "DL", "OL"]
    rows = [_row("KC", position, 1, f"{position} Player") for position in positions]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert len(signals) == 5
    assert acc.result().present == 5


@pytest.mark.parametrize("bad_rank", [0, -1])
async def test_a_rank_below_one_is_dropped(bad_rank):
    rows = [_row("KC", "CB", bad_rank, "A Corner")]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert signals == []
    assert acc.result().present == 0


async def test_a_co_listed_row_resolves_the_ordered_first_candidate():
    """`"Zeta Corner OR Alpha Corner"` is two real players on the chart, and
    resolving the raw, unsplit string would mint one identity that matches
    neither -- the exact fabrication the reviewer demonstrated
    (`fdy-f428ca5d5ff6` for a row backed by no real player). The fix reuses
    `scope.py`'s own `split_co_listed`, so the ordered-first name (by
    normalized-name sort -- "alpha" before "zeta") fills the row's own
    declared rank, and the second name is dropped for this pass rather than
    fabricating a second slot for it."""
    rows = [_row("KC", "CB", 1, "Zeta Corner OR Alpha Corner")]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert len(signals) == 1, "the co-listing must not mint two rows for one rank"
    assert signals[0]["player_id"] == "fdy-alpha-corner"
    assert signals[0]["slot_key"] == "KC:cb_matchup_le_4:1"
    assert acc.result().present == 1


async def test_a_co_listed_row_with_a_single_name_is_unaffected():
    """`split_co_listed` is a no-op for a name that was never co-listed --
    this pins that the fix does not change ordinary rows."""
    rows = [_row("KC", "CB", 1, "A Corner")]
    signals, _ = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert signals[0]["player_id"] == "fdy-a-corner"


async def test_a_dropped_co_listed_counterpart_is_visible_when_the_next_rank_fills():
    """The realistic case: real depth charts list contiguous ranks, so a
    *distinct* row almost always claims the rank right after a co-listing.
    Without an explicit record, the dropped name would then have zero
    trace -- not present (its own rank was never assigned to it), not
    missing (rank 1 reads filled, by the ordered-first name), and, absent
    this test, not in `errors` either. `coverage` would read fully healthy
    while a real player who was genuinely on the chart never appears
    anywhere in the envelope -- a truncated-upstream-reads-as-1.0 bug
    relocated from a slot to a name."""
    rows = [
        _row("KC", "CB", 1, "Zeta Corner OR Alpha Corner"),
        _row("KC", "CB", 2, "Beta Corner"),
    ]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    coverage = acc.result()
    assert len(signals) == 2
    assert coverage.present == 2
    assert "KC:cb_matchup_le_4:1" not in coverage.missing
    assert "KC:cb_matchup_le_4:2" not in coverage.missing, (
        "both slots read filled -- the drop cannot be seen from coverage "
        "numbers alone, which is exactly why it must show up in errors"
    )
    assert any(
        e["reason"] == "co_listed_counterpart_dropped" and "Zeta Corner" in e["detail"]
        for e in acc.errors
    ), acc.errors


async def test_a_deadline_truncates_remaining_rows_rather_than_discarding_them():
    """Checked once per row -- the natural unit for a row-driven loop, where
    `resolve_membership` checks once per team. Cancelling the coroutine
    instead would discard everything already resolved; this preserves it and
    accounts for the rest as `deadline_exceeded`."""
    rows = [
        _row("KC", "CB", 1, "Corner One"),
        _row("KC", "CB", 2, "Corner Two"),
        _row("KC", "CB", 3, "Corner Three"),
    ]
    ticks = iter([NOW, NOW, NOW + timedelta(hours=1)])
    signals, acc = await resolve_matchup_slots(
        rows,
        season=2026,
        week=1,
        now=NOW,
        resolver=StubResolver(),
        deadline=NOW + timedelta(minutes=5),
        clock=lambda: next(ticks),
    )
    assert len(signals) == 2, "the first two rows resolve before the deadline fires"
    coverage = acc.result()
    assert coverage.present == 2
    assert "KC:cb_matchup_le_4:3" in coverage.missing
    assert any(
        e["reason"] == "deadline_exceeded" and e["detail"] == "KC:cb_matchup_le_4:3"
        for e in acc.errors
    )


async def test_no_deadline_means_no_truncation():
    """The default (`deadline=None`) must not truncate anything -- pins that
    adding the parameter did not change behaviour for every existing caller
    that never passes it."""
    rows = [_row("KC", "CB", rank, f"Corner {rank}") for rank in range(1, 5)]
    signals, acc = await resolve_matchup_slots(
        rows, season=2026, week=1, now=NOW, resolver=StubResolver()
    )
    assert len(signals) == 4
    assert acc.result().present == 4

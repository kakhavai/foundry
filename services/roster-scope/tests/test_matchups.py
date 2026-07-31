"""Matchup-scope resolution: coverage seeded from config, never from the
fetch, and the drop-never-guess rule for team/position/rank."""

from datetime import UTC, datetime

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

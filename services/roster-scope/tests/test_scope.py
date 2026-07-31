"""Resolution, the version ledger, grace, change events, and the invariants."""

from datetime import UTC, datetime, timedelta

import pytest
from collector_core.coverage import (
    ERRORS_TRUNCATED,
    MAX_ERRORS,
    CoverageAccumulator,
)

from roster_scope.adapters.depth_chart import (
    DepthChart,
    DepthChartRow,
    parse_depth_chart_csv,
)
from roster_scope.adapters.identity import (
    PlayerRef,
    StubPlayerIdentityResolver,
    UnresolvablePlayer,
)
from roster_scope.rules import expected_slots
from roster_scope.scope import (
    MEMBERSHIP_SIGNAL,
    LedgerUnavailable,
    PreviousScope,
    build_change_events,
    count_stale_depth_charts,
    distinct_rank_violations,
    load_previous_scope,
    ordered_candidates,
    reconcile_missed_producers,
    resolve_membership,
    signal_matches,
    split_co_listed,
)

from .conftest import (
    NOW,
    SpyLake,
    depth_csv,
    depth_row,
    full_league_csv,
    make_membership_envelope,
    membership_row,
)


def charts_from(rows, *, fetched_at=NOW):
    return parse_depth_chart_csv(depth_csv(rows), fetched_at=fetched_at)


def charts_from_csv(text, *, fetched_at=NOW):
    return parse_depth_chart_csv(text, fetched_at=fetched_at)


async def run_resolve(
    charts,
    *,
    previous=None,
    week=1,
    version=1,
    deadline=None,
    clock=None,
    resolver=None,
):
    acc = CoverageAccumulator(expected_slots())
    rows = await resolve_membership(
        charts=charts,
        resolver=resolver or StubPlayerIdentityResolver(),
        previous=previous or PreviousScope(version=0, rows=(), week=None),
        season=2026,
        week=week,
        version=version,
        acc=acc,
        deadline=deadline,
        clock=clock or (lambda: NOW),
    )
    return rows, acc


# --------------------------------------------------------------------------
# Co-listing
# --------------------------------------------------------------------------


def test_co_listing_splits_on_uppercase_or_and_slash():
    assert split_co_listed("Aaron Baker OR Zach Adams") == [
        "Aaron Baker",
        "Zach Adams",
    ]
    assert split_co_listed("Aaron Baker/Zach Adams") == ["Aaron Baker", "Zach Adams"]


def test_co_listing_is_ordered_by_normalized_name_not_upstream_text_order():
    """A chart writing `A OR B` is the upstream *declining* to order them.
    Preserving its text order manufactures a `rank_changed` event every time
    the chart cosmetically flips the pair."""
    flipped = split_co_listed("Zach Adams OR Aaron Baker")
    assert flipped == ["Aaron Baker", "Zach Adams"]
    assert flipped == split_co_listed("Aaron Baker OR Zach Adams")


def test_co_listing_split_is_case_sensitive():
    """No IGNORECASE: a real surname can contain 'or', and folding case would
    shred it into two players that resolve to nothing."""
    assert split_co_listed("Alec Or Brown") == ["Alec Or Brown"]
    assert split_co_listed("Kadarius Toney") == ["Kadarius Toney"]


def test_single_name_passes_through_unchanged():
    assert split_co_listed("Patrick Mahomes") == ["Patrick Mahomes"]
    assert split_co_listed("") == []


# --------------------------------------------------------------------------
# ordered_candidates
# --------------------------------------------------------------------------


def test_ordered_candidates_collapses_media_position_labels():
    charts = charts_from(
        [
            depth_row("KC", "SE", 1, "First Receiver"),
            depth_row("KC", "FL", 2, "Second Receiver"),
            depth_row("KC", "Z", 3, "Third Receiver"),
        ]
    )
    names = [c.name for c in ordered_candidates(charts["KC"], "WR")]
    assert names == ["First Receiver", "Second Receiver", "Third Receiver"]


def _chart_with_jerseys() -> DepthChart:
    """Built directly rather than parsed.

    The nflverse feed carries no jersey column, so a parsed chart can only
    ever produce `None` and would make the co-listing rule below vacuous. The
    rule is about what `ordered_candidates` does with a number when it *has*
    one, so the number has to come from somewhere — a future adapter, or a
    different upstream.
    """
    return DepthChart(
        team="KC",
        captured_at=NOW,
        rows=(
            DepthChartRow("KC", "WR", 1, "Solo Starter", 10),
            DepthChartRow("KC", "WR", 2, "Zed Young OR Abe Older", 88),
        ),
    )


def test_ordered_candidates_expands_a_co_listing_in_place():
    candidates = ordered_candidates(_chart_with_jerseys(), "WR")
    assert [c.name for c in candidates] == [
        "Solo Starter",
        "Abe Older",
        "Zed Young",
    ]


def test_co_listed_rows_drop_the_jersey_number():
    """Which of `A OR B` owns the number the chart printed is genuinely
    ambiguous, and guessing is worse than absent."""
    numbers = {
        c.name: c.jersey_number for c in ordered_candidates(_chart_with_jerseys(), "WR")
    }
    assert numbers == {"Solo Starter": 10, "Abe Older": None, "Zed Young": None}


def test_ordered_candidates_is_empty_without_a_chart():
    assert ordered_candidates(None, "WR") == ()


def test_pinned_out_players_are_removed_and_everyone_below_is_promoted(monkeypatch):
    """`scope.py` binds the config names at import, so the patch target is
    `roster_scope.scope`, not `roster_scope.rules`."""
    monkeypatch.setattr(
        "roster_scope.scope.EXCLUDED_PLAYERS", {"benched guy": "season-ending injury"}
    )
    charts = charts_from(
        [
            depth_row("KC", "WR", 1, "Benched Guy"),
            depth_row("KC", "WR", 2, "Promoted Guy"),
        ]
    )
    assert [c.name for c in ordered_candidates(charts["KC"], "WR")] == ["Promoted Guy"]


# --------------------------------------------------------------------------
# resolve_membership
# --------------------------------------------------------------------------


async def test_a_complete_chart_fills_every_slot():
    rows, acc = await run_resolve(charts_from_csv(full_league_csv()))
    coverage = acc.result()
    assert coverage.expected == 416
    assert coverage.present == 416
    assert coverage.missing == []
    assert coverage.ratio == 1.0
    assert len(rows) == 416


async def test_a_short_chart_contributes_one_missing_slot():
    """The spec's own worked example: a team whose chart yields only three
    receivers under a `WR<=4` rule contributes one entry to coverage.missing."""
    dropped = depth_row("KC", "WR", 4, "KC WR Player4")
    lines = [line for line in full_league_csv().splitlines() if line != dropped]
    assert len(lines) == len(full_league_csv().splitlines()) - 1, (
        "the row this test means to drop was not found — the fixture's shape "
        "changed and this test would otherwise assert nothing"
    )
    rows, acc = await run_resolve(charts_from_csv("\n".join(lines) + "\n"))
    coverage = acc.result()
    assert coverage.expected == 416
    assert coverage.present == 415
    assert coverage.missing == ["KC:wr_depth_le_4:4"]
    assert {"reason": "depth_chart_short", "detail": "KC:wr_depth_le_4:4"} in acc.errors
    assert not any(r["team"] == "KC" and r["depth_rank"] == 4 for r in rows)


async def test_an_unresolvable_name_is_a_missing_slot_never_a_skipped_row():
    """Skipping would shrink numerator and denominator together and read as
    perfect coverage — the collector's whole failure mode."""

    class RefusingResolver:
        async def resolve(self, ref: PlayerRef) -> str:
            if ref.team == "KC" and ref.position == "QB":
                raise UnresolvablePlayer("identity_unresolvable_name", ref.name)
            return await StubPlayerIdentityResolver().resolve(ref)

    rows, acc = await run_resolve(
        charts_from_csv(full_league_csv()), resolver=RefusingResolver()
    )
    coverage = acc.result()
    assert coverage.expected == 416, "the denominator must not move"
    assert coverage.present == 414
    assert coverage.missing == ["KC:qb_depth_le_2:1", "KC:qb_depth_le_2:2"]
    assert not any(r["team"] == "KC" and r["position"] == "QB" for r in rows)
    # Compared as a list, not with `all(...)`: `all([])` is vacuously true, so
    # an `all()` assertion here passes with the `acc.fail` call deleted
    # entirely — which is precisely the "skipped rather than recorded" bug
    # this test claims to protect against. Verified by mutation.
    assert [e["detail"] for e in acc.errors] == [
        "KC:qb_depth_le_2:1",
        "KC:qb_depth_le_2:2",
    ]
    assert [e["reason"] for e in acc.errors] == ["identity_unresolvable_name"] * 2


async def test_total_outage_flows_through_the_same_loop():
    """`charts == {}` is not special-cased. Every human slot fails with a
    classified reason and the 32 config-derived DST slots still fill."""
    rows, acc = await run_resolve({})
    coverage = acc.result()
    assert coverage.expected == 416
    assert coverage.present == 32
    assert round(coverage.ratio, 3) == 0.077
    assert len(rows) == 32
    assert {r["entity_type"] for r in rows} == {"team_defense"}
    # 384 failures, capped to 50 plus a marker that states the true total.
    # The coverage numbers above are the accounting; `errors` is the sample.
    assert len(acc.errors) == MAX_ERRORS + 1
    assert {e["reason"] for e in acc.errors[:MAX_ERRORS]} == {"depth_chart_unavailable"}
    assert acc.errors[-1]["reason"] == ERRORS_TRUNCATED
    assert acc.errors[-1]["total"] == 384
    assert acc.errors[-1]["omitted"] == 384 - MAX_ERRORS


async def test_team_defense_slots_are_config_derived_and_deterministic():
    rows, _ = await run_resolve({})
    ids = {r["team"]: r["player_id"] for r in rows}
    assert ids["KC"] == "fdy-dst-kc"
    assert all(r["depth_source_captured_at"] is None for r in rows)


async def test_the_deadline_truncates_and_records_the_rest():
    """Checked between teams, never by wrapping the pass in a timeout —
    cancelling would discard everything already resolved."""
    ticks = iter([NOW, NOW, NOW + timedelta(hours=1)])
    rows, acc = await run_resolve(
        charts_from_csv(full_league_csv()),
        deadline=NOW + timedelta(minutes=5),
        clock=lambda: next(ticks),
    )
    coverage = acc.result()
    assert coverage.present == 26, "two teams x 13 slots resolved before the deadline"
    assert len(rows) == 26
    assert {e["reason"] for e in acc.errors[:MAX_ERRORS]} == {"deadline_exceeded"}
    assert len(acc.errors) == MAX_ERRORS + 1
    assert acc.errors[-1]["total"] == 390
    # Truncating the *sample* must not truncate the *accounting*.
    assert len(coverage.missing) == 390


async def test_a_manual_override_fills_the_slot_and_is_recorded(monkeypatch):
    from roster_scope.rules import ManualOverride

    monkeypatch.setattr(
        "roster_scope.scope.MANUAL_OVERRIDES",
        (
            ManualOverride(
                team="KC",
                rule_id="wr_depth_le_4",
                rank=4,
                player_name="Pinned Receiver",
                reason="promoted off practice squad, chart lags",
            ),
        ),
    )
    rows, acc = await run_resolve(charts_from_csv(full_league_csv()))
    assert acc.result().present == 416
    pinned = next(
        r
        for r in rows
        if r["team"] == "KC"
        and r["rule_id"] == "wr_depth_le_4"
        and r["depth_rank"] == 4
    )
    assert pinned["is_manual_override"] is True
    assert pinned["override_reason"] == "promoted off practice squad, chart lags"


async def test_rows_carry_the_depth_source_freshness():
    charts = charts_from_csv(full_league_csv(dt="2026-09-14T08:00:00Z"))
    rows, _ = await run_resolve(charts)
    human = next(r for r in rows if r["entity_type"] == "player")
    assert human["depth_source_captured_at"] == "2026-09-14T08:00:00Z"


# --------------------------------------------------------------------------
# Grace and carry-forward
# --------------------------------------------------------------------------


async def test_a_player_who_falls_out_enters_grace_two_weeks_out():
    previous = PreviousScope(
        version=4,
        rows=(membership_row("fdy-gone", team="KC", rank=1, version=4),),
        week=5,
    )
    rows, _ = await run_resolve({}, previous=previous, week=5, version=5)
    grace = next(r for r in rows if r["player_id"] == "fdy-gone")
    assert grace["membership_status"] == "grace"
    assert grace["grace_expires_week"] == 7
    assert grace["previous_depth_rank"] == 1
    assert grace["scope_version"] == 5


async def test_a_grace_row_stays_in_grace_until_its_week_passes():
    previous = PreviousScope(
        version=5,
        rows=(
            membership_row("fdy-gone", version=5, status="grace", grace_expires_week=7),
        ),
        week=6,
    )
    rows, _ = await run_resolve({}, previous=previous, week=7, version=6)
    row = next(r for r in rows if r["player_id"] == "fdy-gone")
    assert row["membership_status"] == "grace"
    assert row["grace_expires_week"] == 7


async def test_a_grace_row_is_excluded_once_its_week_has_passed():
    previous = PreviousScope(
        version=5,
        rows=(
            membership_row("fdy-gone", version=5, status="grace", grace_expires_week=7),
        ),
        week=6,
    )
    rows, _ = await run_resolve({}, previous=previous, week=8, version=6)
    row = next(r for r in rows if r["player_id"] == "fdy-gone")
    assert row["membership_status"] == "excluded"
    assert row["grace_expires_week"] is None


async def test_an_excluded_row_is_emitted_once_and_then_dropped():
    previous = PreviousScope(
        version=6,
        rows=(membership_row("fdy-gone", version=6, status="excluded"),),
        week=8,
    )
    rows, _ = await run_resolve({}, previous=previous, week=9, version=7)
    assert not any(r["player_id"] == "fdy-gone" for r in rows)


async def test_a_returning_player_keeps_their_original_added_at_version():
    charts = charts_from_csv(full_league_csv())
    first, _ = await run_resolve(charts, version=1)
    sample = next(r for r in first if r["entity_type"] == "player")
    previous = PreviousScope(version=1, rows=tuple(first), week=1)
    second, _ = await run_resolve(charts, previous=previous, version=2)
    again = next(r for r in second if r["player_id"] == sample["player_id"])
    assert again["added_at_version"] == 1
    assert again["scope_version"] == 2


# --------------------------------------------------------------------------
# Change events
# --------------------------------------------------------------------------


def test_change_events_cover_the_four_transitions():
    previous = PreviousScope(
        version=1,
        rows=(
            membership_row("fdy-stay", rank=1),
            membership_row("fdy-moved", rank=2),
            membership_row("fdy-dropped", rank=3),
            membership_row("fdy-expiring", status="grace", grace_expires_week=1),
        ),
        week=1,
    )
    rows = [
        membership_row("fdy-stay", rank=1, version=2),
        membership_row("fdy-moved", rank=4, version=2),
        membership_row("fdy-new", rank=2, version=2),
        membership_row(
            "fdy-dropped", rank=3, version=2, status="grace", grace_expires_week=3
        ),
        membership_row("fdy-expiring", rank=1, version=2, status="excluded"),
    ]
    events = build_change_events(previous, rows, version=2, occurred_at=NOW)
    by_id = {e["player_id"]: e for e in events}

    assert "fdy-stay" not in by_id, "an unchanged row must not emit an event"
    assert by_id["fdy-moved"]["transition"] == "rank_changed"
    assert by_id["fdy-moved"]["from_depth_rank"] == 2
    assert by_id["fdy-moved"]["to_depth_rank"] == 4
    assert by_id["fdy-new"]["transition"] == "entered"
    assert by_id["fdy-new"]["from_depth_rank"] is None
    assert by_id["fdy-dropped"]["transition"] == "entered_grace"
    assert by_id["fdy-expiring"]["transition"] == "excluded"
    assert all(e["occurred_at"] == "2026-09-15T12:00:00Z" for e in events)
    assert all(e["scope_version"] == 2 for e in events)


def test_a_player_returning_from_grace_re_enters():
    previous = PreviousScope(
        version=1,
        rows=(membership_row("fdy-back", status="grace", grace_expires_week=3),),
        week=1,
    )
    rows = [membership_row("fdy-back", rank=2, version=2)]
    events = build_change_events(previous, rows, version=2, occurred_at=NOW)
    assert events[0]["transition"] == "entered"


def test_an_override_filled_slot_is_triggered_manual():
    rows = [{**membership_row("fdy-pinned", version=2), "is_manual_override": True}]
    events = build_change_events(
        PreviousScope(version=1, rows=(), week=1), rows, version=2, occurred_at=NOW
    )
    assert events[0]["trigger"] == "manual"


def test_a_first_ever_capture_emits_entered_for_everything():
    rows = [membership_row("fdy-a"), membership_row("fdy-b", rank=2)]
    events = build_change_events(
        PreviousScope(version=0, rows=(), week=None), rows, version=1, occurred_at=NOW
    )
    assert {e["transition"] for e in events} == {"entered"}
    assert len(events) == 2


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def test_duplicate_active_rank_is_a_violation():
    rows = [
        membership_row("fdy-a", team="KC", position="WR", rank=1),
        membership_row("fdy-b", team="KC", position="WR", rank=1),
    ]
    assert distinct_rank_violations(rows) == [
        {"reason": "duplicate_depth_rank", "detail": "KC:WR:1"}
    ]


def test_a_grace_row_may_share_a_rank_with_the_active_row_that_replaced_it():
    """Grace rows deliberately carry their last known rank. Folding them into
    the invariant would make it fire on entirely correct behaviour."""
    rows = [
        membership_row("fdy-active", team="KC", position="WR", rank=1),
        membership_row(
            "fdy-graced",
            team="KC",
            position="WR",
            rank=1,
            status="grace",
            grace_expires_week=3,
        ),
    ]
    assert distinct_rank_violations(rows) == []


def test_the_same_rank_across_different_teams_is_fine():
    rows = [
        membership_row("fdy-a", team="KC", position="WR", rank=1),
        membership_row("fdy-b", team="BUF", position="WR", rank=1),
        membership_row("fdy-c", team="KC", position="TE", rank=1),
    ]
    assert distinct_rank_violations(rows) == []


async def test_a_real_resolution_never_violates_the_rank_invariant():
    rows, _ = await run_resolve(charts_from_csv(full_league_csv()))
    assert distinct_rank_violations(rows) == []


def test_stale_depth_charts_counts_teams_not_an_average():
    charts = charts_from_csv(
        full_league_csv(dt="2026-09-15T00:00:00Z"),
        fetched_at=NOW,
    )
    assert count_stale_depth_charts(charts, NOW) == 0

    frozen = dict(charts)
    frozen["KC"] = type(frozen["KC"])(
        team="KC",
        captured_at=datetime(2026, 9, 1, tzinfo=UTC),
        rows=frozen["KC"].rows,
    )
    assert count_stale_depth_charts(frozen, NOW) == 1


def test_every_team_is_stale_when_no_chart_was_fetched():
    assert count_stale_depth_charts({}, NOW) == 32


def test_reconcile_missed_producers_finds_excluded_players_who_produced():
    membership = [
        membership_row("fdy-excluded", status="excluded"),
        membership_row("fdy-active"),
    ]
    usage = [
        {"player_id": "fdy-excluded", "snap_share": 0.62, "targets": 7, "carries": 0},
        {"player_id": "fdy-active", "snap_share": 0.9},
    ]
    assert reconcile_missed_producers(usage, membership) == ["fdy-excluded"]


def test_reconcile_ignores_an_excluded_player_who_did_not_produce():
    membership = [membership_row("fdy-excluded", status="excluded")]
    usage = [{"player_id": "fdy-excluded", "snap_share": 0, "targets": 0}]
    assert reconcile_missed_producers(usage, membership) == []


def test_reconcile_is_empty_with_no_usage_which_is_todays_state():
    membership = [membership_row("fdy-excluded", status="excluded")]
    assert reconcile_missed_producers([], membership) == []


# --------------------------------------------------------------------------
# The version ledger
# --------------------------------------------------------------------------


def test_ledger_reads_the_newest_envelope_in_the_current_partition():
    lake = SpyLake()
    lake.write(make_membership_envelope([membership_row("fdy-a")], version=1))
    lake.write(
        make_membership_envelope(
            [membership_row("fdy-a", version=2)],
            version=2,
            now=NOW + timedelta(hours=1),
        )
    )
    previous = load_previous_scope(lake, "roster-scope", 2026, 1)
    assert previous.version == 2
    assert previous.week == 1
    assert previous.rows[0]["scope_version"] == 2


def test_ledger_falls_back_to_the_previous_week():
    """Versions stay season-monotonic across a rollover, and — the part that
    matters — grace state crosses the week boundary."""
    lake = SpyLake()
    lake.write(make_membership_envelope([membership_row("fdy-a")], version=9, week=4))
    previous = load_previous_scope(lake, "roster-scope", 2026, 5)
    assert previous.version == 9
    assert previous.week == 4


def test_ledger_is_empty_on_a_cold_lake():
    previous = load_previous_scope(SpyLake(), "roster-scope", 2026, 1)
    assert previous == PreviousScope(version=0, rows=(), week=None)


def test_ledger_does_not_look_before_week_one():
    load_previous_scope(SpyLake(), "roster-scope", 2026, 1)  # must not raise


def test_ledger_failure_raises_rather_than_reading_as_cold():
    """ "No previous scope" and "the lake is down" differ by an entire version
    history; collapsing them resets the sequence to 1 and breaks the
    immutable-additive model."""
    with pytest.raises(LedgerUnavailable):
        load_previous_scope(SpyLake(fail_list=True), "roster-scope", 2026, 1)


def test_ledger_ignores_other_collectors_and_other_signal_types():
    lake = SpyLake()
    lake.write(make_membership_envelope([membership_row("fdy-a")], version=3))
    assert load_previous_scope(lake, "other-collector", 2026, 1).version == 0
    keys = lake.list_keys("roster-scope", MEMBERSHIP_SIGNAL, 2026, 1)
    assert len(keys) == 1


# --------------------------------------------------------------------------
# Row filtering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params,expected",
    [
        ({}, True),
        ({"team": "KC"}, True),
        ({"team": "BUF"}, False),
        ({"position": "WR"}, True),
        ({"rule_id": "wr_depth_le_4"}, True),
        ({"membership_status": "active"}, True),
        ({"membership_status": "grace"}, False),
        ({"player_id": "fdy-a"}, True),
        ({"scope_version": "1"}, True),
        ({"scope_version": "2"}, False),
        ({"team": "KC", "position": "TE"}, False),
    ],
)
def test_signal_matches(params, expected):
    assert signal_matches(membership_row("fdy-a"), params) is expected

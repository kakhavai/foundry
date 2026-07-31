"""The degraded capture paths: the deadline, the metrics, the lake.

Separate from `test_coverage_floor.py`, which is about the expectation itself.
This file is about what a pass does when it cannot finish, and about the three
series that exist because `collector_coverage_ratio` structurally cannot see
their failure modes.
"""

from datetime import UTC, datetime, timedelta

import pytest

from roster_transactions.capture import SIGNAL_TYPE, capture_roster_transactions
from roster_transactions.windows import week_window

from .conftest import SpyLake, capture_with

SEASON, WEEK = 2026, 1
WEEK_START, _ = week_window(SEASON, WEEK)
NOW = WEEK_START + timedelta(days=2)


def _row(*, hours: int, **overrides: str) -> dict[str, str]:
    announced = WEEK_START + timedelta(hours=hours)
    return {
        "transaction_type": "signing",
        "player_id": f"fdy-{hours:04d}",
        "position": "WR",
        "from_team": "",
        "to_team": "KC",
        "announced_at": announced.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "effective_at": (announced + timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "eligible_from_week": "",
        "elevation_count_season": "",
        "confidence": "official",
        "is_void": "false",
        "void_reason": "",
        "supersedes": "",
        "source_ref": f"wire/{hours}",
    } | overrides


async def test_a_pass_over_its_deadline_claims_no_coverage(monkeypatch):
    """It stopped reading the feed partway, so it does not know what the
    intervals it never reached contained. Claiming them would report a
    half-read week as a complete one."""
    envelopes = await capture_with(
        monkeypatch,
        rows=[_row(hours=h) for h in range(1, 6)],
        now=NOW,
        covers_through=NOW,
        deadline=datetime(2000, 1, 1, tzinfo=UTC),
    )

    envelope = envelopes[SIGNAL_TYPE]
    assert envelope.signals == [], "the budget was spent before the first row"
    assert envelope.coverage.present == 0
    assert envelope.coverage.expected > 1
    assert envelope.coverage.ratio == 0.0
    reasons = {error["reason"] for error in envelope.errors}
    assert "deadline_exceeded" in reasons, reasons


async def test_a_pass_inside_its_deadline_is_unaffected(monkeypatch):
    """The companion assertion: without it, a deadline check that fired
    unconditionally would pass the test above and break every real pass."""
    envelopes = await capture_with(
        monkeypatch,
        rows=[_row(hours=h) for h in range(1, 6)],
        now=NOW,
        covers_through=NOW,
        deadline=datetime(2099, 1, 1, tzinfo=UTC),
    )
    envelope = envelopes[SIGNAL_TYPE]
    assert len(envelope.signals) == 5
    assert envelope.coverage.ratio == 1.0
    assert envelope.errors == []


async def test_an_unknown_type_is_counted_on_its_own_series(monkeypatch):
    """A rejected row is not a failed fetch, so
    `collector_capture_failures_total` never moves. Without this gauge a vendor
    renaming `ps_elevation` would be entirely silent."""
    recorded: list[int] = []
    monkeypatch.setattr(
        "roster_transactions.capture.metrics.unknown_transaction_types",
        lambda count: recorded.append(count),
    )
    await capture_with(
        monkeypatch,
        rows=[_row(hours=1), _row(hours=2, transaction_type="practice_squad_promo")],
        now=NOW,
        covers_through=NOW,
    )
    assert recorded == [1]


async def test_the_own_series_are_recorded_even_on_a_quiet_pass(monkeypatch):
    """An absent Prometheus series and a healthy one are indistinguishable in
    PromQL, so a gauge written only when it is interesting cannot be alerted
    on. All three must fire on a pass that captured nothing."""
    recorded: dict[str, int] = {}
    for name in ("rows_captured", "unknown_transaction_types", "duplicate_signings"):
        monkeypatch.setattr(
            f"roster_transactions.capture.metrics.{name}",
            lambda count, _name=name: recorded.__setitem__(_name, count),
        )

    await capture_with(monkeypatch, rows=[], now=NOW, covers_through=NOW)

    assert recorded == {
        "rows_captured": 0,
        "unknown_transaction_types": 0,
        "duplicate_signings": 0,
    }


async def test_duplicate_signings_are_counted_from_the_captured_rows(monkeypatch):
    """End to end for the phase doc's named failure mode: coverage is perfect,
    nothing errors, and only this series says the week is wrong."""
    recorded: list[int] = []
    monkeypatch.setattr(
        "roster_transactions.capture.metrics.duplicate_signings",
        lambda count: recorded.append(count),
    )
    envelopes = await capture_with(
        monkeypatch,
        rows=[
            _row(hours=1, player_id="fdy-dup", confidence="reported"),
            _row(hours=9, player_id="fdy-dup", confidence="official"),
        ],
        now=NOW,
        covers_through=NOW,
    )

    assert recorded == [1]
    assert envelopes[SIGNAL_TYPE].coverage.ratio == 1.0, (
        "the whole point: nothing else can see this"
    )
    assert len(envelopes[SIGNAL_TYPE].signals) == 2


async def test_a_lake_write_failure_surfaces_rather_than_being_swallowed(monkeypatch):
    """The lake is the only durable copy. A pass that served an envelope it
    never managed to write must not report success."""
    with pytest.raises(RuntimeError, match="lake unreachable"):
        await capture_with(
            monkeypatch,
            rows=[_row(hours=1)],
            now=NOW,
            covers_through=NOW,
            lake=SpyLake(fail_write=True),
        )


async def test_the_envelope_scope_names_the_captured_week(monkeypatch):
    envelopes = await capture_with(
        monkeypatch, rows=[], now=NOW, covers_through=NOW, season=2026, week=1
    )
    assert envelopes[SIGNAL_TYPE].scope == {"season": 2026, "week": 1}


async def test_capture_never_touches_the_lake_on_the_event_loop():
    """`build_collector_app` hands every collector an `EventLoopGuardedLake`
    that raises on a synchronous call from the loop thread. Proving the real
    capture path survives it is what stops a readiness inversion — roster-scope
    lost a whole deploy to exactly this."""
    from collector_core.lake import EventLoopGuardedLake

    import httpx

    guarded = EventLoopGuardedLake(SpyLake())
    async with httpx.AsyncClient() as client:
        envelopes = await capture_roster_transactions(
            SEASON, WEEK, client=client, lake=guarded, now=NOW
        )
    assert envelopes[SIGNAL_TYPE].signals, "the placeholder feed must have landed"

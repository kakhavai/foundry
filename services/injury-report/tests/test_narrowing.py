"""Narrowing is behavioural: the assertion is what got fetched and published,
never a flag.

`injury-report`'s registry entry used to argue *against* narrowing at all: an
opposing cornerback ruled out moves a receiver's projection as much as the
receiver's own hamstring does, and defenders never appear on an
offence-oriented watchlist. That is an argument for reading `roster-scope`'s
matchup list TOO -- not for fetching every player in the league. This file
proves the union, not just the membership half.

Three properties, mirroring `usage-share`'s and `player-stats`' own narrowing
suites:

- **Fail closed.** No scope means ZERO upstream calls -- not the schedule
  feed, not the injury feed -- and a `present: 0` envelope for both signal
  types. There is no unnarrowed fallback, not even behind a flag.
- **The union, not either half alone.** A membership-only player and a
  matchup-only defender must both survive; an id in neither list must not.
- **Only `player_injury_status` narrows.** `team_injury_report` is keyed by
  team, not by player, so it is unaffected -- see `test_coverage_floor.py`
  and `adapters/scope.py`'s own docstring for why.
"""

import httpx
import pytest
from collector_core.coverage import MAX_ERRORS
from collector_core.lake import lake_key
from collector_core.scope import ScopeUnavailable

from injury_report import capture as capture_module
from injury_report.capture import SIGNAL_TYPES, capture_injury_report

from .conftest import (
    MATCHUP_SIGNAL_TYPE,
    MEMBERSHIP_SIGNAL_TYPE,
    NOW,
    empty_filing,
    player_ids_in,
    scope_envelope,
    seed_scope,
    wire,
)

PLAYER_SIGNAL = "player_injury_status"
TEAM_SIGNAL = "team_injury_report"


async def _capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_injury_report(
            2026, 1, client=client, lake=lake, now=NOW, **kwargs
        )


def _refusing_upstream(monkeypatch, calls: list):
    """Both upstream adapters, patched to record a call and then explode.

    Loud rather than a plain recorder: a fetch that must not happen should
    not be able to quietly succeed and let the assertion carry the whole
    weight of noticing.
    """

    async def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the upstream must not be reached without a scope")

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", boom)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", boom)


async def test_a_matchup_defender_is_published(lake, monkeypatch):
    """The reason this collector narrows on the UNION. An opposing corner
    ruled out moves a receiver's projection as much as the receiver's own
    hamstring does, and defenders are never on the offensive watchlist."""
    wr_row = wire(team="KC", practice_day="wednesday", player_external_id="kc-wr")
    cb_row = wire(team="KC", practice_day="wednesday", player_external_id="kc-cb")
    wr_id, cb_id = player_ids_in([wr_row]).pop(), player_ids_in([cb_row]).pop()

    async def fake_schedule(*args, **kwargs):
        return {"KC": "2026_01_KC_BUF"}

    async def fake_rows(*args, **kwargs):
        return [wr_row, cb_row]

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    seed_scope(lake, membership={wr_id}, matchup={cb_id})

    envelopes = await _capture(lake)
    assert set(envelopes) == set(SIGNAL_TYPES)
    published = {row["player_id"] for row in envelopes[PLAYER_SIGNAL].signals}

    assert cb_id in published
    assert wr_id in published
    assert len(published) == 2


async def test_an_out_of_scope_player_is_dropped(lake, monkeypatch):
    wr_row = wire(team="KC", practice_day="wednesday", player_external_id="kc-wr")
    cb_row = wire(team="KC", practice_day="wednesday", player_external_id="kc-cb")
    bench_row = wire(
        team="KC", practice_day="wednesday", player_external_id="kc-deep-bench"
    )
    wr_id, cb_id = player_ids_in([wr_row]).pop(), player_ids_in([cb_row]).pop()
    bench_id = player_ids_in([bench_row]).pop()

    async def fake_schedule(*args, **kwargs):
        return {"KC": "2026_01_KC_BUF"}

    async def fake_rows(*args, **kwargs):
        return [wr_row, cb_row, bench_row]

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    seed_scope(lake, membership={wr_id}, matchup={cb_id})

    envelopes = await _capture(lake)
    published = {row["player_id"] for row in envelopes[PLAYER_SIGNAL].signals}

    assert bench_id not in published
    assert published == {wr_id, cb_id}


async def test_a_missing_matchup_scope_fails_closed(lake, monkeypatch):
    """Membership present, matchup absent: must NOT narrow to offence only."""
    membership_only = scope_envelope(MEMBERSHIP_SIGNAL_TYPE, {"fdy-wr"})
    lake.objects[lake_key(membership_only)] = membership_only.to_dict()

    calls: list = []
    _refusing_upstream(monkeypatch, calls)

    with pytest.raises(ScopeUnavailable) as caught:
        await _capture(lake)

    assert calls == []
    assert caught.value.reason == "scope_unavailable"

    written = [e for e in lake.writes if e.signal_type == PLAYER_SIGNAL]
    assert written, "no present:0 envelope was written for the failed pass"
    envelope = written[-1]
    assert envelope.coverage.present == 0
    reasons = [error["reason"] for error in envelope.errors]
    assert any(reason == "scope_unavailable" for reason in reasons), reasons


async def test_an_empty_union_also_fails_closed(lake, monkeypatch):
    """The other reason `fetch_union` can raise: both lists exist but neither
    carries a real member -- `scope_empty`, not `scope_unavailable`, and the
    two must stay distinguishable in the failure envelope."""
    # `seed_scope` always adds the shared anchor so a normal test never trips
    # `scope_empty` by accident; built directly here, with no anchor, to reach
    # the case on purpose -- both lists exist and are readable, and neither
    # names a real member.
    for signal_type in (MEMBERSHIP_SIGNAL_TYPE, MATCHUP_SIGNAL_TYPE):
        envelope = scope_envelope(signal_type, set())
        lake.objects[lake_key(envelope)] = envelope.to_dict()

    calls: list = []
    _refusing_upstream(monkeypatch, calls)

    with pytest.raises(ScopeUnavailable) as caught:
        await _capture(lake)

    assert calls == []
    assert caught.value.reason == "scope_empty"

    written = [e for e in lake.writes if e.signal_type == PLAYER_SIGNAL]
    assert written
    envelope = written[-1]
    assert envelope.coverage.present == 0
    reasons = [error["reason"] for error in envelope.errors]
    assert any(reason == "scope_empty" for reason in reasons), reasons


async def test_a_lake_that_fails_outright_still_writes_a_present_zero_envelope(
    lake, monkeypatch
):
    """`ScopeUnavailable` is only what `ScopeClient` raises when the lake
    answered and had nothing usable. The lake can also fail outright --
    botocore on a dead endpoint, a decode error -- and that must still cost
    zero upstream calls and still leave a `present: 0` envelope behind, per
    the two-except-arms pattern `usage-share` and `player-stats` established."""

    def boom(*args, **kwargs):
        raise RuntimeError("list_objects_v2: endpoint is unreachable")

    monkeypatch.setattr(lake, "list_keys", boom)

    calls: list = []
    _refusing_upstream(monkeypatch, calls)

    with pytest.raises(RuntimeError):
        await _capture(lake)

    assert calls == []

    written = [e for e in lake.writes if e.signal_type == PLAYER_SIGNAL]
    assert written, (
        "a lake failure that is not ScopeUnavailable must still write a "
        "present:0 envelope -- without the second except arm it escapes with "
        "no envelope and no failure counter at all"
    )
    envelope = written[-1]
    assert envelope.coverage.present == 0
    reasons = [error["reason"] for error in envelope.errors]
    assert len(reasons) >= 1, "a failure envelope with no errors explains nothing"
    assert "scope_unavailable" not in reasons, (
        "a lake outage is not an absent scope -- the two have different fixes"
    )


async def test_team_injury_report_is_not_narrowed(lake, monkeypatch):
    """The team-level signal is keyed by team, not player: a club that filed
    must still be visible even when every player it listed is out of scope."""
    bench_row = wire(
        team="KC", practice_day="wednesday", player_external_id="kc-deep-bench"
    )
    bench_id = player_ids_in([bench_row]).pop()

    async def fake_schedule(*args, **kwargs):
        return {"KC": "2026_01_KC_BUF"}

    async def fake_rows(*args, **kwargs):
        return [bench_row]

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    # Neither list names the bench player -- an empty-but-usable union.
    seed_scope(lake, membership=set(), matchup=set())

    envelopes = await _capture(lake)

    assert bench_id not in {
        row["player_id"] for row in envelopes[PLAYER_SIGNAL].signals
    }
    team_rows = [row for row in envelopes[TEAM_SIGNAL].signals if row["team"] == "KC"]
    assert team_rows, "KC filed and must still appear at the team level"
    assert team_rows[0]["filing_status"] == "published"


def _spy_on_scope_dropped_everything(monkeypatch) -> list:
    """Records calls to `metrics.scope_dropped_everything` without touching
    the real OTel counter, so a test can assert the metric fired (or did not)
    independent of the errors array."""
    calls: list = []
    monkeypatch.setattr(
        capture_module.metrics, "scope_dropped_everything", lambda: calls.append(1)
    )
    return calls


async def test_narrowing_to_nothing_is_loud(lake, monkeypatch):
    """Rows offered, none in scope. This must NOT look like a quiet week:
    `coverage.ratio` is team-keyed and cannot tell the two apart on its own,
    so both the errors array and the dedicated counter carry the signal."""
    bench_row = wire(
        team="KC", practice_day="wednesday", player_external_id="kc-deep-bench"
    )

    async def fake_schedule(*args, **kwargs):
        return {"KC": "2026_01_KC_BUF"}

    async def fake_rows(*args, **kwargs):
        return [bench_row]

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    calls = _spy_on_scope_dropped_everything(monkeypatch)
    # A row was offered (the bench player resolves fine); neither list names
    # it, so the union drops the only candidate this pass had.
    seed_scope(lake, membership=set(), matchup=set())

    envelopes = await _capture(lake)

    assert envelopes[PLAYER_SIGNAL].signals == []
    reasons = [error["reason"] for error in envelopes[PLAYER_SIGNAL].errors]
    assert "scope_dropped_everything" in reasons, reasons
    detail = next(
        error["detail"]
        for error in envelopes[PLAYER_SIGNAL].errors
        if error["reason"] == "scope_dropped_everything"
    )
    assert detail.startswith("1 player row(s)"), detail  # one offered, zero survived
    assert len(calls) == 1


async def test_scope_dropped_everything_survives_the_errors_cap(lake, monkeypatch):
    """The identical reasoning `CoverageAccumulator.errors` already applies to
    `below_expected_floor`: the one entry that makes a total narrowing drop
    visible must not be the entry a busy week's error list pushes past the
    `MAX_ERRORS` cap. Sixty unscheduled-feeling clubs file nothing, which
    alone produces `60 * 3 = 180` `report_not_published` entries -- comfortably
    past the cap -- while KC offers one player row that the union drops."""
    teams = {f"T{index:02d}": f"2026_01_T{index:02d}_BYE" for index in range(60)}
    teams["KC"] = "2026_01_KC_BUF"
    bench_row = wire(
        team="KC", practice_day="wednesday", player_external_id="kc-deep-bench"
    )

    async def fake_schedule(*args, **kwargs):
        return teams

    async def fake_rows(*args, **kwargs):
        return [bench_row]

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    seed_scope(lake, membership=set(), matchup=set())

    envelopes = await _capture(lake)

    reasons = [error["reason"] for error in envelopes[PLAYER_SIGNAL].errors]
    # `cap_errors` keeps `MAX_ERRORS` real entries PLUS its own truncation
    # marker as one further entry -- see `collector_core/coverage.py` -- so
    # a genuinely truncated list is `MAX_ERRORS + 1` long, not `MAX_ERRORS`.
    assert len(reasons) == MAX_ERRORS + 1, len(reasons)
    assert "report_not_published" in reasons, reasons  # the cap is genuinely hit
    assert "errors_truncated" in reasons, reasons
    assert reasons[0] == "scope_dropped_everything", reasons[:3]


async def test_a_partial_narrow_does_not_trip_the_total_drop_guard(lake, monkeypatch):
    """Rows offered, SOME in scope: ordinary narrowing, not an anomaly. Most
    of a real week's offered rows are expected to be dropped -- that is the
    whole point of narrowing -- so this must stay quiet."""
    wr_row = wire(team="KC", practice_day="wednesday", player_external_id="kc-wr")
    bench_row = wire(
        team="KC", practice_day="wednesday", player_external_id="kc-deep-bench"
    )
    wr_id = player_ids_in([wr_row]).pop()

    async def fake_schedule(*args, **kwargs):
        return {"KC": "2026_01_KC_BUF"}

    async def fake_rows(*args, **kwargs):
        return [wr_row, bench_row]

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    calls = _spy_on_scope_dropped_everything(monkeypatch)
    seed_scope(lake, membership={wr_id}, matchup=set())

    envelopes = await _capture(lake)

    published = {row["player_id"] for row in envelopes[PLAYER_SIGNAL].signals}
    assert published == {wr_id}
    reasons = [error["reason"] for error in envelopes[PLAYER_SIGNAL].errors]
    assert "scope_dropped_everything" not in reasons, reasons
    assert calls == []


async def test_a_genuinely_quiet_week_does_not_trip_the_total_drop_guard(
    lake, monkeypatch
):
    """No player rows offered at all -- nobody hurt, nothing to narrow. This
    is the case the guard exists to leave alone: a quiet week and a total
    narrowing drop must stay distinguishable, not both trip the same alarm."""
    quiet_filing = empty_filing(team="KC", practice_day="wednesday")

    async def fake_schedule(*args, **kwargs):
        return {"KC": "2026_01_KC_BUF"}

    async def fake_rows(*args, **kwargs):
        return [quiet_filing]

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    calls = _spy_on_scope_dropped_everything(monkeypatch)
    seed_scope(lake, membership=set(), matchup=set())

    envelopes = await _capture(lake)

    assert envelopes[PLAYER_SIGNAL].signals == []
    reasons = [error["reason"] for error in envelopes[PLAYER_SIGNAL].errors]
    assert "scope_dropped_everything" not in reasons, reasons
    assert calls == []

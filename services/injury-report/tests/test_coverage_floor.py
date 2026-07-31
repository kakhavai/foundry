"""Coverage, which for this collector is the thing most likely to be got wrong.

Two failures are being defended against here and they pull in opposite
directions:

1. **A pass that captured almost nothing must not report 1.0.** `expected` must
   never derive from what the injury feed returned, so the denominator comes
   from the schedule upstream and the clock, floored by a declared constant.
2. **A club with nobody hurt must not read as an outage.** An empty filing is a
   complete observation and counts as coverage *present*; only a club that
   filed nothing is missing.

Conflating those two in either direction is the hole this collector exists to
close, so both have their own test below.
"""

from datetime import timedelta

import httpx
import pytest

from injury_report.capture import (
    EXPECTED_FLOOR,
    MIN_SCHEDULED_TEAMS,
    SIGNAL_TYPES,
    capture_injury_report,
)
from injury_report.report import practice_days_elapsed

from .conftest import NOW, SpyLake, empty_filing, wire

# NOW is a Tuesday, so the whole report week has elapsed.
DAYS = practice_days_elapsed(NOW)
FULL_FLOOR = MIN_SCHEDULED_TEAMS * len(DAYS)


async def _capture(scheduled, rows, monkeypatch, lake, *, now=NOW, deadline=None):
    async def fake_schedule(*args, **kwargs):
        return dict(scheduled)

    async def fake_rows(*args, **kwargs):
        return list(rows)

    monkeypatch.setattr("injury_report.capture.fetch_scheduled_games", fake_schedule)
    monkeypatch.setattr("injury_report.capture.fetch_report_rows", fake_rows)
    async with httpx.AsyncClient() as client:
        return await capture_injury_report(
            2026, 1, client=client, lake=lake, now=now, deadline=deadline
        )


def _week(*teams: str) -> dict[str, str]:
    return {team: f"2026_01_{team}" for team in teams}


def _filings(team: str, **overrides) -> list[dict]:
    return [wire(team=team, practice_day=day, **overrides) for day in DAYS]


def _real_errors(envelope) -> list[dict]:
    """Errors other than the declared-floor shortfall, which several of these
    fixtures trip on purpose because they schedule fewer than 26 clubs."""
    return [e for e in envelope.errors if e["reason"] != "below_expected_floor"]


async def test_a_truncated_schedule_does_not_report_full_coverage(monkeypatch):
    """The floor's whole job. A schedule upstream that answers with one club
    must not yield `expected: 3, present: 3`, ratio 1.0 — twenty-five other
    clubs' reports would have vanished with nothing to show for it."""
    envelopes = await _capture(_week("KC"), _filings("KC"), monkeypatch, SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    assert envelopes, "no envelopes makes every assertion below vacuous"
    for envelope in envelopes.values():
        assert envelope.coverage.expected == FULL_FLOOR
        assert envelope.coverage.present == len(DAYS)
        assert envelope.coverage.ratio < 1.0
        reasons = {error["reason"] for error in envelope.errors}
        assert "below_expected_floor" in reasons, reasons


async def test_a_total_outage_of_the_injury_feed_reports_a_low_ratio(monkeypatch):
    """Every club scheduled, not one filing received. The catastrophic case:
    it must read as zero coverage, never as a healthy quiet week."""
    teams = _week(*(f"T{index:02d}" for index in range(32)))
    envelopes = await _capture(teams, [], monkeypatch, SpyLake())
    assert len(envelopes) == len(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == 32 * len(DAYS)
        assert envelope.coverage.ratio == 0.0
        assert len(envelope.coverage.missing) == 32 * len(DAYS)
        # 96 identical errors would be 96 near-identical entries in memory, in
        # every HTTP response and in every lake object, so the array is capped
        # at 50 with an explicit marker. The marker is the load-bearing part: a
        # silently truncated error list looks like a short list of problems.
        reasons = [error["reason"] for error in envelope.errors]
        assert len(reasons) == 51
        assert set(reasons) == {"report_not_published", "errors_truncated"}, reasons
        assert envelope.errors[-1]["omitted"] == 32 * len(DAYS) - 50
        assert envelope.signals == []


async def test_a_genuinely_empty_report_is_not_an_outage(monkeypatch):
    """A club with nobody hurt files a report listing nobody. That is a
    complete observation: coverage present, a `team_injury_report` row saying
    `empty`, and no error at all. Reading it as an outage would make every
    healthy club look like a broken feed."""
    teams = _week("KC", "BUF")
    rows = [
        *[empty_filing(team="KC", practice_day=day) for day in DAYS],
        *_filings("BUF", player_external_id="buf-01"),
    ]
    envelopes = await _capture(teams, rows, monkeypatch, SpyLake())

    assert len(envelopes) == len(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.coverage.present == 2 * len(DAYS)
        assert envelope.coverage.missing == []
        assert _real_errors(envelope) == []

    team_rows = envelopes["team_injury_report"].signals
    kc = [row for row in team_rows if row["team"] == "KC"]
    assert len(kc) == len(DAYS)
    assert {row["filing_status"] for row in kc} == {"empty"}
    assert {row["player_count"] for row in kc} == {0}
    # The empty club contributes no player rows, which is correct: it reported
    # nobody, not everybody-healthy-with-rows.
    player_rows = envelopes["player_injury_status"].signals
    assert player_rows, "the other club's rows are what make this comparison real"
    assert {row["team"] for row in player_rows} == {"BUF"}


async def test_a_club_that_filed_nothing_is_missing_not_healthy(monkeypatch):
    """The companion to the test above, and the reason it is not enough on its
    own: absence must be recorded as absence, with a reason."""
    teams = _week("KC", "BUF")
    envelopes = await _capture(teams, _filings("KC"), monkeypatch, SpyLake())

    envelope = envelopes["team_injury_report"]
    assert envelope.coverage.missing == sorted(f"BUF:{day}" for day in DAYS)
    reasons = [error["reason"] for error in _real_errors(envelope)]
    assert reasons == ["report_not_published"] * len(DAYS), reasons
    assert not [row for row in envelope.signals if row["team"] == "BUF"]


async def test_expansion_past_the_floor_still_reports_honestly(monkeypatch):
    """The floor must raise a short count, never cap a genuine one."""
    teams = _week(*(f"T{index:02d}" for index in range(34)))
    rows = [row for team in teams for row in _filings(team)]
    envelopes = await _capture(teams, rows, monkeypatch, SpyLake())
    assert len(envelopes) == len(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.coverage.expected == 34 * len(DAYS)
        assert envelope.coverage.expected > FULL_FLOOR
        assert envelope.coverage.ratio == 1.0


@pytest.mark.parametrize(
    ("days_after_wednesday", "expected_days"),
    [
        (0, 1),  # Wednesday — only Wednesday's practice has happened
        (1, 2),  # Thursday
        (2, 3),  # Friday, the final report
        (4, 3),  # Sunday, game day
        (6, 3),  # Tuesday, the tail of the same report week
    ],
)
def test_the_denominator_tracks_the_practice_week_not_the_feed(
    days_after_wednesday, expected_days
):
    """`expected` scales with days elapsed, read off the clock. Reading it off
    the filings instead would make a Friday on which nobody filed look exactly
    like a Wednesday — three missing reports would become zero."""
    wednesday = NOW + timedelta(days=1)  # NOW is a Tuesday
    assert wednesday.weekday() == 2
    days = practice_days_elapsed(wednesday + timedelta(days=days_after_wednesday))
    assert len(days) == expected_days


def test_the_practice_week_never_expects_nothing():
    """`Coverage.ratio` returns 1.0 when `expected` is 0, so a day on which
    nothing is owed would report perfect coverage for a pass that captured
    nothing. No day of the week may produce an empty tuple."""
    for offset in range(7):
        moment = NOW + timedelta(days=offset)
        days = practice_days_elapsed(moment)
        assert days, f"weekday {moment.weekday()} expects nothing"


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert EXPECTED_FLOOR, "an empty floor table makes every check below vacuous"
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())
    # 32 clubs minus the six ever on a bye in one week. Pinned so that
    # "simplifying" it into a count of something fetched fails here.
    assert MIN_SCHEDULED_TEAMS == 26

"""The degraded paths through `capture.py`, and what it records on every pass.

`test_coverage_floor.py` owns the expectation arithmetic. This file owns the
branches that only run when something has gone wrong upstream, plus the two
metrics whose whole job is to be alertable — and a metric written only when it
is interesting cannot be alerted on, so "recorded on every pass, including
zero" is asserted rather than assumed.
"""

import httpx
import pytest
import respx
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream

from player_stats.adapters.identity import UnresolvedPlayer
from player_stats.adapters.upstream import BoxRow, stats_url
from player_stats.capture import (
    BOX_SIGNAL,
    _team_matches_game,
    capture_player_stats,
)

from .conftest import (
    NOW,
    SEASON,
    WEEK,
    SpyLake,
    feed_csv,
    feed_row,
    seed_scope,
    stub_player_id,
)


class RecordingMetrics:
    """Captures what `capture.py` recorded, without an OTel reader."""

    def __init__(self) -> None:
        self.restatement_calls: list[int] = []
        self.identity_miss_calls: list[int] = []
        self.coverage_ratios: list[float] = []
        self.attempts = 0
        self.failures: list[BaseException] = []

    def capture_attempt(self):
        self.attempts += 1

    def capture_failure(self, exc, reason=None):
        self.failures.append(exc)

    def coverage(self, signal_type, ratio):
        self.coverage_ratios.append(ratio)

    def staleness(self, seconds):
        pass

    def restatements(self, count):
        self.restatement_calls.append(count)

    def identity_misses(self, count):
        self.identity_miss_calls.append(count)

    @staticmethod
    def reason_for(exc):
        return "malformed"


@pytest.fixture
def recorded(monkeypatch) -> RecordingMetrics:
    spy = RecordingMetrics()
    monkeypatch.setattr("player_stats.capture.metrics", spy)
    return spy


async def capture(rows, lake=None, **kwargs):
    lake = SpyLake() if lake is None else lake
    # The team-defense-anchor-only default (see `conftest.seed_scope`): an
    # empty player watchlist, successfully fetched, matching the old unscoped
    # baseline these tests were written against.
    seed_scope(lake)
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(200, text=feed_csv(rows))
        )
        async with httpx.AsyncClient() as client:
            return await capture_player_stats(
                SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
            )


def seed(lake, *, yards, revision, stat_state, captured_at=None):
    """Put one prior snapshot in the lake for this partition."""
    captured_at = captured_at or NOW.replace(hour=11)
    lake.write(
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="player-stats",
            signal_type=BOX_SIGNAL,
            captured_at=captured_at,
            upstream=Upstream(adapter="test", fetched_at=captured_at),
            scope={"season": SEASON, "week": WEEK},
            coverage=Coverage(expected=1, present=1, missing=[]),
            errors=[],
            signals=[
                {
                    "player_id": stub_player_id("00-0000001"),
                    "game_id": f"{SEASON}_01_BUF_KC",
                    "passing": {
                        "attempts": 0,
                        "completions": 0,
                        "yards": 0,
                        "touchdowns": 0,
                        "interceptions": 0,
                        "sacks_taken": 0,
                        "air_yards": 0,
                    },
                    "rushing": {
                        "attempts": 0,
                        "yards": 0,
                        "touchdowns": 0,
                        "first_downs": 0,
                    },
                    "receiving": {
                        "targets": 5,
                        "receptions": 3,
                        "yards": yards,
                        "touchdowns": 0,
                        "air_yards": 0,
                        "yards_after_catch": 0,
                        "first_downs": 0,
                    },
                    "misc": {
                        "fumbles_lost": 0,
                        "two_point_conversions": 0,
                        "return_touchdowns": 0,
                    },
                    "revision": revision,
                    "stat_state": stat_state,
                }
            ],
        )
    )


def receiver(yards: int) -> dict:
    return feed_row(
        player_id="00-0000001",
        position="WR",
        targets=5,
        receptions=3,
        receiving_yards=yards,
    )


# ── metrics recorded on every pass ────────────────────────────────────────────


async def test_both_custom_metrics_are_recorded_even_when_zero(recorded):
    """An absent Prometheus series and a healthy one are indistinguishable in
    PromQL, so a value only written when it is interesting is unalertable."""
    await capture([receiver(70)])
    assert recorded.restatement_calls == [0]
    assert recorded.identity_miss_calls == [0]
    assert recorded.attempts == 1
    assert len(recorded.coverage_ratios) == 1


async def test_a_restatement_is_counted(recorded):
    """The upstream changed a value the lake already captured — the whole
    reason `revision` exists."""
    lake = SpyLake()
    seed(lake, yards=70, revision=0, stat_state=None)
    envelopes = await capture([receiver(84)], lake)
    assert recorded.restatement_calls == [1]
    assert envelopes[BOX_SIGNAL].signals[0]["revision"] == 1


async def test_an_identical_recapture_counts_no_restatement(recorded):
    """'an adapter re-emitting unchanged rows as new revisions' is what the
    spec says a `player_stats_restatements_total` spike usually means."""
    lake = SpyLake()
    seed(lake, yards=70, revision=2, stat_state=None)
    envelopes = await capture([receiver(70)], lake)
    assert recorded.restatement_calls == [0]
    assert envelopes[BOX_SIGNAL].signals[0]["revision"] == 2


async def test_a_restated_final_row_is_flagged_in_the_errors(recorded):
    """The spec's hard assertion, made visible: a box score the upstream had
    certified `final` changed anyway."""
    lake = SpyLake()
    seed(lake, yards=70, revision=1, stat_state="final")
    envelope = (await capture([receiver(84)], lake))[BOX_SIGNAL]
    assert "final_row_restated" in {error["reason"] for error in envelope.errors}
    assert envelope.signals[0]["revision"] == 2


# ── identity misses ───────────────────────────────────────────────────────────


async def test_an_unresolvable_row_is_counted_and_named(recorded, monkeypatch):
    """Not dropped silently: the phase doc calls a silent join failure the one
    most likely to go unnoticed for a season."""

    class RefusingCrosswalk:
        async def resolve(self, upstream_player_id):
            raise UnresolvedPlayer("identity_not_in_crosswalk", upstream_player_id)

    monkeypatch.setattr(
        "player_stats.capture.build_id_resolver", lambda client: RefusingCrosswalk()
    )
    envelope = (await capture([receiver(70)]))[BOX_SIGNAL]

    assert envelope.signals == []
    assert recorded.identity_miss_calls == [1]
    assert "identity_not_in_crosswalk" in {e["reason"] for e in envelope.errors}
    # Never expected, because no canonical key exists to name — the row is
    # visible in `errors`, not silently absent from both.
    assert envelope.coverage.missing == []


async def test_a_row_that_cannot_be_built_costs_one_key(recorded, monkeypatch):
    """A bug in `build_signal` must fail one row, not the whole pass."""

    def boom(*args, **kwargs):
        raise ValueError("bad row")

    monkeypatch.setattr("player_stats.capture.build_signal", boom)
    envelope = (await capture([receiver(70)]))[BOX_SIGNAL]

    assert envelope.signals == []
    assert len(envelope.coverage.missing) == 1
    assert "malformed" in {error["reason"] for error in envelope.errors}


# ── the team/game predicate ───────────────────────────────────────────────────


def box(team: str, game_id: str) -> BoxRow:
    return BoxRow(
        upstream_player_id="00-0000001",
        player_name="Test",
        game_id=game_id,
        team=team,
        opponent="BUF",
        position="WR",
        passing={},
        rushing={},
        receiving={},
        misc={},
    )


def test_a_team_in_its_own_game_id_matches():
    assert _team_matches_game(box("KC", "2026_01_BUF_KC")) is True
    assert _team_matches_game(box("BUF", "2026_01_BUF_KC")) is True


def test_a_team_that_played_neither_side_does_not_match():
    assert _team_matches_game(box("DEN", "2026_01_BUF_KC")) is False


def test_a_blank_team_or_game_id_does_not_match():
    """Absent is not ambiguous-but-fine: with nothing to check against, the
    only honest answer is that the row cannot be attributed."""
    assert _team_matches_game(box("", "2026_01_BUF_KC")) is False
    assert _team_matches_game(box("KC", "")) is False

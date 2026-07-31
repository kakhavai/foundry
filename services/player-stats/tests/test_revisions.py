"""Revision derivation and the `/revisions` series.

The spec's failure mode for this collector: "a stat correction issued days
after a game silently changes a value the lake already captured... two
snapshots for the same `(game_id, player_id)` now disagree and nothing in the
envelope says which one wins." These are the tests that hold the guard.
"""

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream

from player_stats.revisions import (
    SinceFormatError,
    build_revision_series,
    final_restated,
    fingerprint,
    load_previous_state,
    next_revision,
    parse_since,
    row_key,
)

from .conftest import NOW, SEASON, WEEK, SpyLake

SIGNAL_TYPE = "player_box_weekly"


def signal(*, player_id="fdy-a1", yards=100, revision=0, stat_state=None):
    return {
        "player_id": player_id,
        "game_id": f"{SEASON}_01_BUF_KC",
        "receiving": {"yards": yards, "receptions": 5, "targets": 8},
        "passing": {},
        "rushing": {},
        "misc": {},
        "revision": revision,
        "stat_state": stat_state,
        # Derived blocks, deliberately outside the fingerprint.
        "fantasy_points": {"ppr": yards / 10},
        "rates": {"catch_rate": 0.625},
    }


def snapshot(lake, rows, *, captured_at=NOW):
    envelope = Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="player-stats",
        signal_type=SIGNAL_TYPE,
        captured_at=captured_at,
        upstream=Upstream(adapter="test", fetched_at=captured_at),
        scope={"season": SEASON, "week": WEEK},
        coverage=Coverage(expected=len(rows), present=len(rows), missing=[]),
        errors=[],
        signals=rows,
    )
    lake.write(envelope)
    return envelope


# ── the fingerprint ───────────────────────────────────────────────────────────


def test_key_order_does_not_change_the_fingerprint():
    """A dict rebuilt in another order is the same box score."""
    a = {"passing": {"yards": 1, "tds": 2}, "rushing": {}, "receiving": {}, "misc": {}}
    b = {"misc": {}, "receiving": {}, "rushing": {}, "passing": {"tds": 2, "yards": 1}}
    assert fingerprint(a) == fingerprint(b)


def test_a_changed_counting_stat_changes_the_fingerprint():
    assert fingerprint(signal(yards=100)) != fingerprint(signal(yards=101))


def test_a_derived_block_does_not_change_the_fingerprint():
    """`rates` and `fantasy_points` are computed from the counting stats, so
    including them would double-count — and a scoring-table change would then
    read as the whole league being restated."""
    base = signal()
    changed = {**base, "fantasy_points": {"ppr": 999.0}, "rates": {"catch_rate": 0.1}}
    assert fingerprint(base) == fingerprint(changed)


def test_the_row_key_is_game_and_player():
    assert row_key(signal(player_id="fdy-x")) == f"{SEASON}_01_BUF_KC|fdy-x"


# ── deriving the next revision ────────────────────────────────────────────────


def test_a_first_sighting_is_revision_zero_and_not_a_restatement():
    assert next_revision({}, signal()) == (0, False)


def test_an_identical_recapture_does_not_increment():
    """The spec's named alert: 'an adapter re-emitting unchanged rows as new
    revisions' is what makes `player_stats_restatements_total` spike."""
    previous = {
        row_key(signal()): {
            "revision": 3,
            "fingerprint": fingerprint(signal()),
            "stat_state": None,
        }
    }
    assert next_revision(previous, signal()) == (3, False)


def test_a_changed_stat_increments_by_exactly_one():
    previous = {
        row_key(signal()): {
            "revision": 3,
            "fingerprint": fingerprint(signal(yards=100)),
            "stat_state": None,
        }
    }
    assert next_revision(previous, signal(yards=112)) == (4, True)


def test_revisions_are_monotonic_across_a_chain_of_corrections():
    """Monotonic per `(game_id, player_id)` is the spec's hard requirement."""
    previous: dict[str, dict] = {}
    seen = []
    for yards in (100, 112, 112, 118, 118, 121):
        row = signal(yards=yards)
        revision, _ = next_revision(previous, row)
        seen.append(revision)
        previous[row_key(row)] = {
            "revision": revision,
            "fingerprint": fingerprint(row),
            "stat_state": None,
        }
    assert seen == [0, 1, 1, 2, 2, 3]
    assert seen == sorted(seen)


def test_a_final_row_that_changes_is_flagged():
    """'a row once emitted as stat_state: final never changes again without
    revision incrementing' — the change itself is what must be visible."""
    previous = {
        row_key(signal()): {
            "revision": 1,
            "fingerprint": fingerprint(signal(yards=100)),
            "stat_state": "final",
        }
    }
    assert final_restated(previous, signal(yards=118)) is True
    assert next_revision(previous, signal(yards=118))[0] == 2


def test_an_unchanged_final_row_is_not_flagged():
    previous = {
        row_key(signal()): {
            "revision": 1,
            "fingerprint": fingerprint(signal()),
            "stat_state": "final",
        }
    }
    assert final_restated(previous, signal()) is False


def test_a_provisional_row_that_changes_is_not_flagged_as_a_final_restatement():
    previous = {
        row_key(signal()): {
            "revision": 1,
            "fingerprint": fingerprint(signal(yards=100)),
            "stat_state": "provisional",
        }
    }
    assert final_restated(previous, signal(yards=118)) is False


# ── reading the previous snapshot ─────────────────────────────────────────────


def test_an_absent_partition_is_an_empty_state_not_a_failure():
    state = load_previous_state(SpyLake(), "player-stats", SIGNAL_TYPE, SEASON, WEEK)
    assert state == {}


def test_only_the_newest_snapshot_is_read():
    """Revisions accumulate forward, so the newest object already carries the
    highest revision for every key — reading the history would cost one object
    read per capture that ever ran."""
    lake = SpyLake()
    snapshot(lake, [signal(yards=100, revision=0)], captured_at=NOW)
    snapshot(
        lake,
        [signal(yards=118, revision=1)],
        captured_at=NOW.replace(hour=13),
    )
    state = load_previous_state(lake, "player-stats", SIGNAL_TYPE, SEASON, WEEK)
    assert len(state) == 1
    assert state[row_key(signal())]["revision"] == 1


def test_a_lake_read_failure_propagates():
    """`capture.py` turns this into a failure envelope: minting revision 0 over
    rows already at revision 3 would break monotonicity."""
    with pytest.raises(RuntimeError):
        load_previous_state(
            SpyLake(fail_read=True), "player-stats", SIGNAL_TYPE, SEASON, WEEK
        )


# ── the /revisions series ─────────────────────────────────────────────────────


def test_a_first_appearance_is_not_a_restatement():
    """The generator asked what changed; `/signals` answers what exists."""
    lake = SpyLake()
    snapshot(lake, [signal(revision=0)])
    assert build_revision_series(lake, "player-stats", SIGNAL_TYPE, SEASON, WEEK) == []


def test_an_increased_revision_is_emitted_once():
    lake = SpyLake()
    snapshot(lake, [signal(yards=100, revision=0)], captured_at=NOW)
    snapshot(lake, [signal(yards=118, revision=1)], captured_at=NOW.replace(hour=13))
    series = build_revision_series(lake, "player-stats", SIGNAL_TYPE, SEASON, WEEK)
    assert len(series) == 1
    assert series[0]["revision"] == 1
    assert series[0]["player_id"] == "fdy-a1"
    assert series[0]["captured_at"] == "2026-09-15T13:00:00Z"


def test_an_unchanged_recapture_emits_nothing():
    lake = SpyLake()
    snapshot(lake, [signal(revision=0)], captured_at=NOW)
    snapshot(lake, [signal(revision=0)], captured_at=NOW.replace(hour=13))
    assert build_revision_series(lake, "player-stats", SIGNAL_TYPE, SEASON, WEEK) == []


def test_since_filters_out_older_restatements():
    lake = SpyLake()
    snapshot(lake, [signal(yards=100, revision=0)], captured_at=NOW)
    snapshot(lake, [signal(yards=118, revision=1)], captured_at=NOW.replace(hour=13))
    snapshot(lake, [signal(yards=121, revision=2)], captured_at=NOW.replace(hour=18))

    everything = build_revision_series(lake, "player-stats", SIGNAL_TYPE, SEASON, WEEK)
    assert [entry["revision"] for entry in everything] == [1, 2]

    recent = build_revision_series(
        lake,
        "player-stats",
        SIGNAL_TYPE,
        SEASON,
        WEEK,
        since=NOW.replace(hour=15),
    )
    assert [entry["revision"] for entry in recent] == [2]


# ── ?since= parsing ───────────────────────────────────────────────────────────


def test_since_accepts_rfc_3339_with_a_z_suffix():
    parsed = parse_since("2026-09-15T13:00:00Z")
    assert parsed is not None
    assert parsed.isoformat() == "2026-09-15T13:00:00+00:00"


def test_a_naive_since_is_assumed_utc_rather_than_rejected():
    parsed = parse_since("2026-09-15T13:00:00")
    assert parsed is not None and parsed.tzinfo is not None


def test_an_absent_since_means_everything():
    assert parse_since(None) is None
    assert parse_since("   ") is None


def test_a_mistyped_since_is_refused_not_ignored():
    """Ignoring it would return the whole history, which a caller reads as
    'nothing has been restated in my window' — the opposite of the truth."""
    with pytest.raises(SinceFormatError):
        parse_since("last tuesday")

"""`build_diff` — the ordering changes between two captures.

Exercised against `SpyLake` directly rather than through the route, so the
diffing itself is tested without a background capture in the way. The route's
own wiring (auth, the 404, the offload) is proved in `test_routes.py`.
"""

from datetime import UTC, datetime, timedelta

import pytest
from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream

from depth_chart.capture import CHART_SIGNAL
from depth_chart.diff import SnapshotNotFound, build_diff

from .conftest import SpyLake

FIRST = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
SECOND = FIRST + timedelta(hours=1)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def row(team, position, name, rank, gsis_id=None):
    return {
        "team": team,
        "position": position,
        "player_name": name,
        "official_rank": rank,
        "gsis_id": gsis_id,
    }


def write(lake, at: datetime, rows: list[dict]) -> None:
    lake.write(
        Envelope(
            envelope_version=ENVELOPE_VERSION,
            collector="depth-chart",
            signal_type=CHART_SIGNAL,
            captured_at=at,
            upstream=Upstream(adapter="test", fetched_at=at),
            scope={"season": 2026, "week": 1},
            coverage=Coverage(expected=160, present=len(rows)),
            errors=[],
            signals=rows,
        )
    )


def diff(lake, **kwargs):
    return build_diff(
        lake,
        "depth-chart",
        CHART_SIGNAL,
        2026,
        1,
        from_captured_at=stamp(FIRST),
        to_captured_at=stamp(SECOND),
        **kwargs,
    )


def test_an_unchanged_ordering_produces_no_groups():
    lake = SpyLake()
    rows = [row("KC", "WR", "A", 1, "00-1"), row("KC", "WR", "B", 2, "00-2")]
    write(lake, FIRST, rows)
    write(lake, SECOND, rows)
    result = diff(lake)
    assert result["count"] == 0
    assert result["groups"] == []


def test_a_promotion_is_named_as_one():
    """Named by direction rather than left as a delta: a promotion is the event
    a consumer is watching for."""
    lake = SpyLake()
    write(lake, FIRST, [row("KC", "WR", "A", 1, "00-1"), row("KC", "WR", "B", 2, "00-2")])
    write(lake, SECOND, [row("KC", "WR", "B", 1, "00-2"), row("KC", "WR", "A", 2, "00-1")])

    result = diff(lake)
    assert len(result["groups"]) == 1
    changes = {c["player_name"]: c for c in result["groups"][0]["changes"]}
    assert len(changes) == 2
    assert changes["B"]["change"] == "promoted"
    assert changes["B"]["from_rank"] == 2 and changes["B"]["to_rank"] == 1
    assert changes["A"]["change"] == "demoted"


def test_arrivals_and_departures_are_distinguished():
    lake = SpyLake()
    write(lake, FIRST, [row("KC", "RB", "Old", 1, "00-1")])
    write(lake, SECOND, [row("KC", "RB", "New", 1, "00-9")])

    changes = diff(lake)["groups"][0]["changes"]
    assert len(changes) == 2
    assert {(c["player_name"], c["change"]) for c in changes} == {
        ("Old", "removed"),
        ("New", "added"),
    }


def test_a_spelling_correction_is_not_a_roster_move():
    """Keyed on the crosswalk id where the feed supplied one, so a vendor
    fixing a name does not read as one departure plus one arrival."""
    lake = SpyLake()
    write(lake, FIRST, [row("KC", "TE", "Travis Kelce", 1, "00-7")])
    write(lake, SECOND, [row("KC", "TE", "Travis Kelce Sr.", 1, "00-7")])
    assert diff(lake)["count"] == 0


def test_rows_without_a_crosswalk_id_fall_back_to_the_name():
    lake = SpyLake()
    write(lake, FIRST, [row("KC", "QB", "A Passer", 1)])
    write(lake, SECOND, [row("KC", "QB", "A Passer", 2)])
    changes = diff(lake)["groups"][0]["changes"]
    assert len(changes) == 1
    assert changes[0]["change"] == "demoted"


def test_only_groups_that_changed_are_reported():
    lake = SpyLake()
    steady = row("BUF", "QB", "Steady", 1, "00-5")
    write(lake, FIRST, [steady, row("KC", "QB", "A", 1, "00-1")])
    write(lake, SECOND, [steady, row("KC", "QB", "B", 1, "00-2")])

    groups = diff(lake)["groups"]
    assert len(groups) == 1
    assert groups[0]["team"] == "KC"


def test_a_missing_snapshot_raises_rather_than_returning_an_empty_diff():
    lake = SpyLake()
    write(lake, FIRST, [row("KC", "QB", "A", 1, "00-1")])
    with pytest.raises(SnapshotNotFound) as excinfo:
        diff(lake)
    assert stamp(SECOND) in str(excinfo.value)


def test_only_the_two_named_objects_are_read():
    """A prefix scan lists the partition; the diff must not then read all of
    it. At a volatile cadence a week's partition holds hundreds of objects."""
    lake = SpyLake()
    for offset in range(6):
        write(
            lake,
            FIRST + timedelta(hours=offset),
            [row("KC", "QB", f"Passer {offset}", 1, f"00-{offset}")],
        )

    reads: list[str] = []
    inner = lake.read

    def counting_read(key):
        reads.append(key)
        return inner(key)

    lake.read = counting_read
    diff(lake)
    assert len(reads) == 2, reads

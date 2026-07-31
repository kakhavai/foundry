"""The coverage floor, which is the thing most likely to be got wrong.

`coverage.expected` must never derive from what a fetch returned. These are the
tests that fail if somebody "simplifies" `capture.py` by computing the
expectation from the histories it just folded.

Coverage here counts `(team, position)` **groups**, not players, straight from
the spec: *"a team publishing a chart with a position group omitted registers as
missing"*.
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from depth_chart.adapters.upstream import fetch_depth_charts, source_ref
from depth_chart.capture import (
    CHART_SIGNAL,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    STABILITY_SIGNAL,
    capture_depth_chart,
)
from depth_chart.stability import STALE_AFTER_DAYS
from depth_chart.universe import POSITIONS, TEAMS, expected_groups

from .conftest import (
    CURRENT_DT,
    NOW,
    SpyLake,
    depth_csv,
    depth_row,
    dt_plus_days,
    full_chart_rows,
)

FEED = source_ref(2026, 1)


async def capture(rows, lake=None, **kwargs):
    respx.get(FEED).mock(return_value=httpx.Response(200, text=depth_csv(rows)))
    async with httpx.AsyncClient() as client:
        return await capture_depth_chart(
            2026, 1, client=client, lake=lake or SpyLake(), now=NOW, **kwargs
        )


def test_the_floor_is_derived_from_config_not_from_a_document():
    """32 teams x 5 configured positions = 160, computed in `universe.py`
    before any upstream is contacted."""
    assert len(TEAMS) == 32
    assert len(POSITIONS) == 5
    assert len(expected_groups()) == 160
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert EXPECTED_FLOOR[CHART_SIGNAL] == 160
    assert EXPECTED_FLOOR[STABILITY_SIGNAL] == 160


@respx.mock
async def test_a_truncated_upstream_does_not_report_full_coverage():
    """The failure this floor exists for. A feed carrying one team of 32 must
    not yield `expected: 5, present: 5`, ratio 1.0 — which is exactly what
    deriving the expectation from the fetch would produce."""
    rows = [depth_row("KC", position, 1, f"KC {position}") for position in POSITIONS]
    envelopes = await capture(rows)
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.present == 5
        assert envelope.coverage.ratio < 1.0
        assert len(envelope.coverage.missing) == 155


@respx.mock
async def test_a_total_outage_reports_a_low_ratio_not_a_perfect_one():
    """A feed that parses but describes nobody. `0/0` reads as ratio 1.0, which
    is correct for a bye week and catastrophic here — the floor is what makes
    the denominator survive an empty document."""
    envelopes = await capture([])
    assert envelopes
    for signal_type, envelope in envelopes.items():
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[signal_type]
        assert envelope.coverage.ratio == 0.0
        assert len(envelope.coverage.missing) == EXPECTED_FLOOR[signal_type]


@respx.mock
async def test_one_omitted_position_group_registers_as_missing():
    """Straight from the spec's `coverage.expected` definition. A chart that
    publishes every team's QB room but no TE room is 32 groups short, and the
    missing list has to name them."""
    rows = [
        depth_row(team, position, 1, f"{team} {position}")
        for team in TEAMS
        for position in POSITIONS
        if position != "TE"
    ]
    envelopes = await capture(rows)
    missing = envelopes[CHART_SIGNAL].coverage.missing
    assert len(missing) == 32
    assert all(key.endswith(":TE") for key in missing)


@respx.mock
async def test_a_deeper_chart_is_not_more_coverage():
    """Coverage counts groups. Six receivers where the config expects a group
    must not read as six units of coverage, or a chart padding one room could
    mask another going dark."""
    rows = full_chart_rows() + [
        depth_row("KC", "WR", rank, f"KC WR {rank}") for rank in range(2, 7)
    ]
    envelopes = await capture(rows)
    chart = envelopes[CHART_SIGNAL]
    assert chart.coverage.expected == EXPECTED_FLOOR[CHART_SIGNAL]
    assert chart.coverage.present == EXPECTED_FLOOR[CHART_SIGNAL]
    # More signal ROWS than groups — the rows grew, the coverage did not.
    assert len(chart.signals) == EXPECTED_FLOOR[CHART_SIGNAL] + 5


@respx.mock
async def test_an_elapsed_deadline_leaves_groups_missing_and_says_why():
    """A truncated pass that reports itself truncated is useful; one that
    reports itself complete is not.

    The deadline is a **wall-clock** budget, so it is built from
    `datetime.now`, not from the suite's frozen `NOW` — which is in the future
    and would make this assert nothing.
    """
    envelopes = await capture(
        full_chart_rows(), deadline=datetime.now(tz=UTC) - timedelta(seconds=1)
    )
    assert envelopes
    for envelope in envelopes.values():
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        reasons = {error["reason"] for error in envelope.errors}
        assert "capture_truncated" in reasons, reasons


@respx.mock
async def test_a_deadline_elapsing_during_mapping_leaves_groups_missing(monkeypatch):
    """Distinct from the fetch-side truncation above: here the feed arrives
    intact and the budget runs out while groups are being mapped.

    The adapter is stubbed for this one case, because the fetch-side deadline
    check fires first by construction and would leave nothing to map — which
    means this branch is only reachable with the fetch replaced.
    """
    respx.get(FEED).mock(
        return_value=httpx.Response(200, text=depth_csv(full_chart_rows()))
    )
    async with httpx.AsyncClient() as client:
        prefetched = await fetch_depth_charts(2026, 1, client=client, now=NOW)
    assert len(prefetched.histories) == EXPECTED_FLOOR[CHART_SIGNAL]

    async def already_read(*args, **kwargs):
        return prefetched

    monkeypatch.setattr("depth_chart.capture.fetch_depth_charts", already_read)
    async with httpx.AsyncClient() as client:
        envelopes = await capture_depth_chart(
            2026,
            1,
            client=client,
            lake=SpyLake(),
            now=NOW,
            deadline=datetime.now(tz=UTC) - timedelta(seconds=1),
        )

    for envelope in envelopes.values():
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        reasons = {error["reason"] for error in envelope.errors}
        assert "deadline_exceeded" in reasons, reasons


@respx.mock
async def test_a_stale_chart_is_captured_and_flagged_rather_than_dropped():
    """A frozen chart is still the best available ordering, so it is published
    — with `is_stale` set. Dropping it would turn a detectable staleness
    problem into an undetectable coverage hole."""
    frozen = dt_plus_days(CURRENT_DT, -(STALE_AFTER_DAYS + 20))
    envelopes = await capture(full_chart_rows(dt=frozen))
    stability = envelopes[STABILITY_SIGNAL]
    assert stability.coverage.ratio == 1.0
    assert stability.signals
    assert all(row["is_stale"] for row in stability.signals)
    assert len(stability.signals) == EXPECTED_FLOOR[STABILITY_SIGNAL]


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert len(EXPECTED_FLOOR) == 2
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())

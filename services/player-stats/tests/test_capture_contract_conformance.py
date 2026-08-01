"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.

Every test here goes through `respx` and the real streaming adapter, so the CSV
parse, the coverage accounting, the crosswalk, the revision derivation and the
schema are exercised as one thing rather than around a mock of the middle.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from jsonschema import Draft202012Validator, FormatChecker

from player_stats.adapters.upstream import stats_url
from player_stats.capture import (
    BOX_SIGNAL,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    capture_player_stats,
)

from .conftest import NOW, SEASON, WEEK, SpyLake, feed_csv, feed_row, seed_scope

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "player-stats.json").read_text()
)["signal_types"]


def validate(envelopes: dict) -> int:
    """Validate every envelope and every row. Returns the rows validated.

    The count is returned rather than discarded because `for row in []` passes
    vacuously — a caller asserting on it is what stops this being decoration.
    """
    validated = 0
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for row in body["signals"]:
            validator.validate(row)
            validated += 1
    return validated


REALISTIC_WEEK = [
    feed_row(
        player_id="00-0000001",
        player_display_name="Test Quarterback",
        position="QB",
        attempts=32,
        completions=21,
        passing_yards=274,
        passing_tds=2,
        passing_interceptions=1,
        sacks_suffered=2,
        passing_air_yards=310,
        carries=3,
        rushing_yards=14,
    ),
    feed_row(
        player_id="00-0000002",
        player_display_name="Test Receiver",
        position="WR",
        targets=11,
        receptions=8,
        receiving_yards=104,
        receiving_tds=1,
        receiving_air_yards=121,
        receiving_yards_after_catch=38,
        receiving_first_downs=5,
    ),
    feed_row(
        player_id="00-0000003",
        player_display_name="Test Kicker",
        position="K",
        fg_att=3,
        pat_att=2,
    ),
]


async def capture(lake, rows=None, **kwargs):
    """One capture through the real streaming adapter."""
    # Team-defense-anchor-only (see `conftest.seed_scope`): an empty player
    # watchlist, successfully fetched, so these tests exercise the box-score
    # path exactly as before narrowing shipped on.
    seed_scope(lake)
    document = feed_csv(REALISTIC_WEEK if rows is None else rows)
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(
            return_value=httpx.Response(200, text=document)
        )
        async with httpx.AsyncClient() as client:
            return await capture_player_stats(
                SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
            )


async def capture_failing(lake):
    """One capture whose upstream is unreachable.

    The scope is seeded so the failure genuinely comes from the mocked
    upstream `ConnectError` rather than from `fetch_watchlist` raising first
    — narrowing is fetched before the upstream, so an unseeded lake here
    would turn this into a scope-failure test by accident.
    """
    seed_scope(lake)
    with respx.mock:
        respx.get(stats_url(SEASON)).mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient() as client:
            return await capture_player_stats(
                SEASON, WEEK, client=client, lake=lake, now=NOW
            )


async def test_a_complete_capture_conforms():
    envelopes = await capture(SpyLake())
    assert set(envelopes) == set(SIGNAL_TYPES)
    assert validate(envelopes) == len(REALISTIC_WEEK)


async def test_every_emitted_row_carries_the_full_field_set():
    """A required field silently dropped by `build_signal` would still validate
    against a schema nothing ever fed a row to."""
    envelopes = await capture(SpyLake())
    rows = envelopes[BOX_SIGNAL].signals
    assert len(rows) == len(REALISTIC_WEEK)
    required = set(FIELD_SCHEMAS[BOX_SIGNAL]["required"])
    for row in rows:
        assert required <= set(row), required - set(row)


async def test_fantasy_points_are_computed_here_not_read_off_the_upstream():
    """This is the only collector permitted to compute fantasy points, and the
    feed publishes its own `fantasy_points` columns that are deliberately
    ignored. 274 pass yds (10.96) + 2 pass TD (8) - 1 INT + 14 rush yds (1.4)."""
    envelopes = await capture(SpyLake())
    quarterback = next(
        row for row in envelopes[BOX_SIGNAL].signals if row["position"] == "QB"
    )
    assert quarterback["fantasy_points"]["standard"] == 19.36
    assert quarterback["fantasy_points"]["ppr"] == 19.36
    assert quarterback["scoring_rules_version"]


async def test_a_reception_heavy_row_differs_across_formats():
    envelopes = await capture(SpyLake())
    receiver = next(
        row for row in envelopes[BOX_SIGNAL].signals if row["position"] == "WR"
    )
    points = receiver["fantasy_points"]
    assert points["ppr"] - points["standard"] == 8.0
    assert points["half_ppr"] - points["standard"] == 4.0


async def test_unavailable_fields_are_null_rather_than_fabricated():
    """The feed carries neither a snap count nor a certification level. A `0`
    would read as 'dressed but never played', and a guessed `stat_state` would
    be an inference dressed up as the upstream's own certification."""
    envelopes = await capture(SpyLake())
    rows = envelopes[BOX_SIGNAL].signals
    assert len(rows) == len(REALISTIC_WEEK)
    for row in rows:
        assert row["offense_snaps"] is None
        assert row["stat_state"] is None
        assert row["played"] is True


async def test_a_complete_capture_reports_coverage_against_the_floor():
    """`expected` is floored independently of the fetch, so a healthy pass is
    judged against the declared universe rather than against itself."""
    envelope = (await capture(SpyLake()))[BOX_SIGNAL]
    assert envelope.coverage.present == len(REALISTIC_WEEK)
    assert envelope.coverage.expected == EXPECTED_FLOOR[BOX_SIGNAL]


async def test_the_envelope_is_written_to_the_lake():
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    lake = SpyLake()
    await capture(lake)
    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == len(SIGNAL_TYPES)


async def test_a_failed_capture_writes_a_present_zero_envelope():
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""
    lake = SpyLake()
    with pytest.raises(httpx.ConnectError):
        await capture_failing(lake)

    assert {e.signal_type for e in lake.writes} == set(SIGNAL_TYPES)
    assert len(lake.writes) == len(SIGNAL_TYPES)
    for envelope in lake.writes:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        assert envelope.errors, "a failure envelope with no errors explains nothing"


async def test_a_total_outage_reports_a_low_ratio_not_a_perfect_one():
    """`Coverage.ratio` returns 1.0 when `expected` is 0. A capture that
    reached nothing must therefore floor to the declared universe, or a
    complete outage alerts as perfect health."""
    lake = SpyLake()
    with pytest.raises(httpx.ConnectError):
        await capture_failing(lake)
    assert len(lake.writes) == 1
    assert lake.writes[0].coverage.ratio == 0.0


async def test_an_unreadable_lake_fails_the_pass_rather_than_resetting_revisions():
    """Continuing without the previous snapshot would mint revision 0 over rows
    already at revision 3, breaking the monotonicity consumers pin against."""
    lake = SpyLake(fail_read=True)
    with pytest.raises(RuntimeError):
        await capture(lake)
    assert len(lake.writes) == 1
    assert lake.writes[0].coverage.present == 0
    assert {e["reason"] for e in lake.writes[0].errors} == {
        "previous_snapshot_unavailable"
    }


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)
